"""Render a plan frame- and sample-exact in one ffmpeg decode pass.

Pieces are selected by frame index (video) and sample index (audio); stream
copy is never used because it can only cut on keyframes. Audio gets a short
length-preserving fade at each cut, then two-pass loudness normalization to
-14 LUFS / -1 dBTP (or gain + delay-compensated limiter when linear
normalization cannot meet the peak ceiling). Output follows YouTube's
recommended H.264 High / closed GOP / AAC 48 kHz / faststart settings.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from fractions import Fraction

from openjarvis.tools.video import core
from openjarvis.tools.video import effects as fx
from openjarvis.tools.video.planner import PRESETS


def _pre_filters(plan, deinterlace):
    parts = []
    if deinterlace:
        parts.append("bwdif=mode=send_frame")
    if plan.get("vfr"):
        parts.append("fps=%s" % plan["fps"])
    return parts


def _video_select(plan, idx, geo, deinterlace):
    """No transitions: one input, frames picked by index in a single pass."""
    fps = core.to_fraction(plan["fps"])
    parts = _pre_filters(plan, deinterlace)
    terms = "+".join(
        "between(n\\,%d\\,%d)" % (s["src_start_frame"], s["src_end_frame"] - 1)
        for s in plan["segments"]
    )
    parts.append("select='%s'" % terms)
    parts.append("setpts=N/(%d/%d)/TB" % (fps.numerator, fps.denominator))
    return ["[%d:v:0]%s[vasm]" % (idx, ",".join(parts + geo))]


def _video_transitions(plan, piece_inputs, geo, deinterlace):
    """Transitions: each piece is its own exactly-seeked input, joined by xfade.

    Separate inputs keep memory flat: a piece is decoded only when the chain
    reaches it, instead of buffering every later piece's frames.
    """
    fps = core.to_fraction(plan["fps"])
    lines = []
    for i, (s, idx) in enumerate(zip(plan["segments"], piece_inputs)):
        parts = (
            _pre_filters(plan, deinterlace)
            + [
                "trim=end_frame=%d" % s["frames"],
                "settb=%d/%d" % (fps.denominator, fps.numerator),
                "setpts=N",
            ]
            + geo
            + ["format=yuv420p"]
        )
        lines.append("[%d:v:0]%s[p%d]" % (idx, ",".join(parts), i))
    acc = "p0"
    for i, s in enumerate(plan["segments"][1:], start=1):
        nxt = "j%d" % i
        if s["trans_in_frames"]:
            lines.append(
                "[%s][p%d]xfade=transition=%s:duration=%.9f:offset=%.9f[%s]"
                % (
                    acc,
                    i,
                    s["trans_in_type"],
                    float(core.frame_time(s["trans_in_frames"], fps)),
                    float(core.frame_time(s["out_start_frame"], fps)),
                    nxt,
                )
            )
        else:
            # concat emits a microsecond timebase; restore 1/fps so a later
            # xfade sees matching inputs and frame numbers stay exact.
            lines.append(
                "[%s][p%d]concat=n=2:v=1:a=0,settb=%d/%d,setpts=N[%s]"
                % (acc, i, fps.denominator, fps.numerator, nxt)
            )
        acc = nxt
    lines.append("[%s]null[vasm]" % acc)
    return lines


def _audio_chain(plan, loudnorm_filter, idx=0, music_idx=None, effects=()):
    segs = plan["segments"]
    sr = plan["sample_rate"]
    fps = core.to_fraction(plan["fps"])
    fade_s = Fraction(plan["audio"].get("crossfade_ms", 12)) / 1000
    lines = []
    k = len(segs)
    if k == 1:
        lines.append("[%d:a:0]anull[as0]" % idx)
    else:
        lines.append(
            "[%d:a:0]asplit=%d%s" % (idx, k, "".join("[as%d]" % i for i in range(k)))
        )

    def hard_cut(i):  # join into piece i is a plain cut between non-contiguous pieces
        return (
            i > 0
            and not segs[i].get("trans_in_frames")
            and segs[i - 1]["src_end_frame"] != segs[i]["src_start_frame"]
        )

    for i, s in enumerate(segs):
        n = s["audio_end_sample"] - s["audio_start_sample"]
        dur = Fraction(n, sr)
        f = min(fade_s, dur / 3)
        start, end = s["audio_start_sample"], s["audio_end_sample"]
        lead = max(0, -start)  # audio begins after this piece's first frame
        chain = [
            "atrim=start_sample=%d:end_sample=%d" % (max(0, start), max(0, end)),
            "asetpts=PTS-STARTPTS",
        ]
        if lead:
            chain.append("adelay=delays=%dS:all=1" % lead)
        if f > 0 and hard_cut(i):
            chain.append("afade=t=in:d=%.6f" % float(f))
        if f > 0 and i + 1 < k and hard_cut(i + 1):
            chain.append("afade=t=out:st=%.6f:d=%.6f" % (float(dur - f), float(f)))
        # Pin each piece to its exact sample count (pads a short final read).
        chain.append("apad=whole_len=%d" % n)
        chain.append("atrim=end_sample=%d" % n)
        lines.append("[as%d]%s[ab%d]" % (i, ",".join(chain), i))

    if any(s.get("trans_in_frames") for s in segs):
        # Sample-exact crossfades. ffmpeg's acrossfade rounds the overlap to its
        # internal frame size (measured: +256 samples, ~5 ms of drift), so the
        # overlap is cut out with atrim, faded, summed and re-joined instead.
        # Overlap length comes from the same output frame timeline as the video.
        acc = "ab0"
        acc_len = segs[0]["audio_end_sample"] - segs[0]["audio_start_sample"]
        for i in range(1, k):
            prev, cur = segs[i - 1], segs[i]
            n = cur["audio_end_sample"] - cur["audio_start_sample"]
            nxt = "aj%d" % i
            if cur["trans_in_frames"]:
                prev_end = core.frame_of(
                    core.frame_time(prev["out_end_frame"], fps), sr
                )
                cur_start = core.frame_of(
                    core.frame_time(cur["out_start_frame"], fps), sr
                )
                ov = max(1, min(prev_end - cur_start, acc_len, n))
                # A fades out over its last ov samples; B fades in over its first
                # ov, is delayed by exactly acc_len - ov samples, and the two sum.
                lines += [
                    "[%s]afade=t=out:start_sample=%d:ns=%d[xa%d]"
                    % (acc, acc_len - ov, ov, i),
                    "[ab%d]afade=t=in:ns=%d,adelay=delays=%dS:all=1[xb%d]"
                    % (i, ov, acc_len - ov, i),
                    "[xa%d][xb%d]amix=inputs=2:duration=longest:normalize=0,"
                    "atrim=end_sample=%d[%s]" % (i, i, acc_len - ov + n, nxt),
                ]
                acc_len += n - ov
            else:
                lines.append("[%s][ab%d]concat=n=2:v=0:a=1[%s]" % (acc, i, nxt))
                acc_len += n
            acc = nxt
        lines.append("[%s]aresample=48000[voice]" % acc)
    else:
        joined = "concat=n=%d:v=0:a=1," % k if k > 1 else ""
        lines.append(
            "%s%saresample=48000[voice]"
            % ("".join("[ab%d]" % i for i in range(k)), joined)
        )
    total = core.frame_of(core.frame_time(plan["total_frames"], fps), 48000)
    music = fx.music_effect(effects)
    cur = "voice"
    if music and music_idx is not None:
        mchain = [
            "aresample=48000",
            "volume=%.2fdB" % music["volume_db"],
            "atrim=end_sample=%d" % total,
            "asetpts=PTS-STARTPTS",
        ]
        if music["fade_in"] > 0:
            mchain.append("afade=t=in:d=%.3f" % music["fade_in"])
        if music["fade_out"] > 0:
            mchain.append(
                "afade=t=out:st=%.6f:d=%.3f"
                % (max(0.0, total / 48000.0 - music["fade_out"]), music["fade_out"])
            )
        lines.append("[%d:a:0]%s[mus]" % (music_idx, ",".join(mchain)))
        if music["duck"]:
            # Sidechain: the voice drives a compressor on the music bed.
            lines.append("[voice]asplit=2[vmain][vkey]")
            lines.append(
                "[mus][vkey]sidechaincompress=threshold=0.02:ratio=12:attack=15:"
                "release=400[mduck]"
            )
            lines.append(
                "[vmain][mduck]amix=inputs=2:duration=first:normalize=0[mixed]"
            )
        else:
            lines.append("[voice][mus]amix=inputs=2:duration=first:normalize=0[mixed]")
        cur = "mixed"
    tail = []
    if loudnorm_filter:
        tail += [loudnorm_filter, "aresample=48000"]
    tail += fx.audio_fade_filters(plan, effects)
    # Loudness filters may add or drop a few ms (loudnorm frames, resampler
    # flush); pin the programme to the sample count the video timeline implies.
    tail += ["apad=whole_len=%d" % total, "atrim=end_sample=%d" % total]
    lines.append("[%s]%s[aout]" % (cur, ",".join(tail)))
    return ";\n".join(lines)


def _gain_limiter(cfg, measured, extra_headroom_db=0.0):
    """Fallback when linear loudnorm cannot hit the target under the peak ceiling.

    Static gain to the target, then a lookahead limiter (delay-compensated, so
    timing is untouched) at the ceiling minus 1 dB of headroom for inter-sample
    peaks and the AAC encode. The verifier re-measures the result.
    """
    gain = float(cfg.get("target_lufs", -14.0)) - float(measured["input_i"])
    ceiling_db = float(cfg.get("true_peak_db", -1.0)) - 1.0 - extra_headroom_db
    # Limit at 4x oversampling so inter-sample peaks are caught (true-peak style).
    return (
        "volume=%.2fdB,aresample=192000,alimiter=limit=%.5f:attack=5:release=50:"
        "level=disabled:latency=1,aresample=48000" % (gain, 10 ** (ceiling_db / 20))
    )


def _linear_possible(cfg, measured):
    gain = float(cfg.get("target_lufs", -14.0)) - float(measured["input_i"])
    return float(measured["input_tp"]) + gain <= float(cfg.get("true_peak_db", -1.0))


def _loudnorm(cfg, measured=None):
    base = "loudnorm=I=%s:TP=%s:LRA=%s" % (
        cfg.get("target_lufs", -14.0),
        cfg.get("true_peak_db", -1.0),
        cfg.get("lra", 11.0),
    )
    if measured is None:
        return base + ":print_format=json"
    return base + (
        ":measured_I=%s:measured_TP=%s:measured_LRA=%s:measured_thresh=%s:"
        "offset=%s:linear=true:print_format=json"
        % (
            measured["input_i"],
            measured["input_tp"],
            measured["input_lra"],
            measured["input_thresh"],
            measured["target_offset"],
        )
    )


def _parse_loudnorm(stderr):
    text = stderr.decode("utf-8", "replace")
    m = re.search(r"\{\s*\"input_i\"[^{}]*\}", text)
    if not m:
        raise core.ToolError("loudnorm produced no measurement", core.EXIT_FINDINGS)
    return json.loads(m.group(0))


def _color_args(plan_src_info, out_height):
    v = plan_src_info.get("video") or {}
    if v.get("color_primaries") or v.get("color_space") or v.get("color_transfer"):
        return []  # tagged source: x264 inherits the tags from the decoded frames
    if out_height and out_height >= 720:
        return [
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
        ]
    return []


def _build(plan, info, preset, deinterlace, lfilter, tmp, measure=False, audio=True):
    """-> (input args, graph). measure=True: audio only; audio=False: video only."""
    src = plan["source"]
    fps = core.to_fraction(plan["fps"])
    effects = plan.get("effects") or []
    inputs, graph = [], []

    def add(args):
        inputs.append(args)
        return len(inputs) - 1

    audio_idx = None
    if plan["has_video"] and not measure:
        geo, size = fx.geometry_filters(info, PRESETS[preset])
        if fx.has_transitions(plan):
            piece_inputs = []
            for s in plan["segments"]:
                f0 = s["src_start_frame"]
                seek = (Fraction(f0) - Fraction(1, 2)) / fps if f0 else None
                args = (["-ss", "%.9f" % float(seek)] if seek else []) + ["-i", src]
                piece_inputs.append(add(args))
            graph += _video_transitions(plan, piece_inputs, geo, deinterlace)
        else:
            audio_idx = add(["-i", src])
            graph += _video_select(plan, audio_idx, geo, deinterlace)
        fx_graph, label = fx.effects_video_graph(plan, effects, size, "vasm", tmp)
        if fx_graph:
            graph.append(fx_graph)
        graph.append("[%s]format=yuv420p[vout]" % label)
    if plan["has_audio"] and audio:
        if audio_idx is None:
            audio_idx = add(["-i", src])
        music = fx.music_effect(effects)
        music_idx = add(["-stream_loop", "-1", "-i", music["file"]]) if music else None
        graph.append(_audio_chain(plan, lfilter, audio_idx, music_idx, effects))
    return [a for arg in inputs for a in arg], ";\n".join(graph)


def render(
    plan,
    out_path,
    preset=None,
    force=False,
    dry_run=False,
    deinterlace=None,
    loudness=True,
    keep_graph=None,
):
    if plan.get("verdict") == "BLOCKED":
        raise core.ToolError(
            "plan is BLOCKED; fix its block findings and re-plan first",
            core.EXIT_BLOCKED,
        )
    if not plan.get("segments"):
        raise core.ToolError("plan has no segments", core.EXIT_BLOCKED)
    src = plan["source"]
    if os.path.abspath(out_path) == os.path.abspath(src):
        raise core.ToolError("refusing to write over the source file")
    if os.path.exists(out_path) and not force and not dry_run:
        raise core.ToolError("%s exists; pass --force to overwrite" % out_path)
    ffmpeg, _ = core.require_ffmpeg()
    info = core.probe(src)
    preset = preset or plan["output"].get("preset", "youtube")
    if preset not in PRESETS:
        raise core.ToolError("unknown preset %r" % preset)
    cfg = PRESETS[preset]
    fps = core.to_fraction(plan["fps"])
    if deinterlace is None:
        deinterlace = bool(
            info.get("video")
            and info["video"].get("field_order") not in (None, "progressive", "unknown")
        )

    has_v, has_a = plan["has_video"], plan["has_audio"]
    report = {
        "output": os.path.abspath(out_path),
        "preset": preset,
        "passes": [],
        "transitions": sum(1 for s in plan["segments"] if s.get("trans_in_frames")),
        "effects": [e["type"] for e in plan.get("effects") or []],
    }
    t0 = time.time()
    measured = None
    lcfg = plan["audio"].get("loudness") or {}
    with tempfile.TemporaryDirectory() as tmp:
        if has_a and loudness and lcfg:
            in_args, graph = _build(
                plan, info, preset, deinterlace, _loudnorm(lcfg), tmp, measure=True
            )
            gpath = os.path.join(tmp, "measure.txt")
            with open(gpath, "w") as fh:
                fh.write(graph)
            cmd = (
                [ffmpeg, "-hide_banner", "-nostdin"]
                + in_args
                + ["-filter_complex_script", gpath, "-map", "[aout]", "-f", "null", "-"]
            )
            if dry_run:
                report["passes"].append(
                    {"pass": "loudness-measure", "graph": graph, "cmd": cmd}
                )
            else:
                measured = _parse_loudnorm(core.run(cmd).stderr)
                report["loudness_measured"] = measured
                report["passes"].append(
                    {"pass": "loudness-measure", "seconds": round(time.time() - t0, 2)}
                )
        # Audio first, to its own AAC file: the encoded result is measured, and
        # if the AAC encode pushed true peak over the ceiling, the limiter is
        # tightened by exactly the measured overshoot and the audio re-encoded.
        audio_file = os.path.join(tmp, "audio.m4a")
        if has_a:
            channels = (info.get("audio") or {}).get("channels") or 2
            kbps = cfg["audio_kbps"] if channels >= 2 else cfg["audio_kbps"] // 2
            headroom = 0.0
            tp_target = float(lcfg.get("true_peak_db", -1.0)) if lcfg else 0.0
            for attempt in range(3):
                lfilter = None
                if loudness and lcfg:
                    if measured and (headroom or not _linear_possible(lcfg, measured)):
                        lfilter = _gain_limiter(lcfg, measured, headroom)
                        report["loudness_mode"] = "gain+limiter"
                    else:
                        lfilter = _loudnorm(
                            lcfg,
                            measured
                            or {
                                "input_i": "X",
                                "input_tp": "X",
                                "input_lra": "X",
                                "input_thresh": "X",
                                "target_offset": "X",
                            },
                        )
                        report["loudness_mode"] = "linear"
                in_args, agraph = _build(
                    plan, info, preset, deinterlace, lfilter, tmp, measure=True
                )
                apath = os.path.join(tmp, "audio.txt")
                with open(apath, "w") as fh:
                    fh.write(agraph)
                acmd = (
                    [ffmpeg, "-hide_banner", "-nostdin", "-y"]
                    + in_args
                    + [
                        "-filter_complex_script",
                        apath,
                        "-map",
                        "[aout]",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "%dk" % kbps,
                        "-ar",
                        "48000",
                        audio_file,
                    ]
                )
                if dry_run:
                    report["passes"].append(
                        {"pass": "audio", "graph": agraph, "cmd": acmd}
                    )
                    break
                core.run(acmd)
                if not (loudness and lcfg and measured):
                    break
                got = core.measure_loudness(audio_file).get("true_peak_dbtp")
                report["audio_true_peak_dbtp"] = got
                if got is None or got <= tp_target + 0.3:
                    break
                headroom += got - tp_target + 0.2
                report["passes"].append(
                    {
                        "pass": "audio-retry",
                        "true_peak": got,
                        "extra_headroom_db": round(headroom, 2),
                    }
                )
        in_args, graph = _build(plan, info, preset, deinterlace, None, tmp, audio=False)
        gpath = keep_graph or os.path.join(tmp, "render.txt")
        with open(gpath, "w") as fh:
            fh.write(graph)
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y" if force else "-n"] + in_args
        if has_a:
            cmd += ["-i", audio_file]
        cmd += ["-filter_complex_script", gpath]
        if has_v:
            gop = max(1, round(fps / 2))
            out_h = fx.output_size(info, cfg)[1]
            cmd += [
                "-map",
                "[vout]",
                "-c:v",
                "libx264",
                "-preset",
                cfg["x264"],
                "-crf",
                str(cfg["crf"]),
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-g",
                str(gop),
                "-keyint_min",
                str(gop),
                "-bf",
                "2",
                "-x264-params",
                "open-gop=0:scenecut=0",
                "-r",
                plan["fps"],
                "-fps_mode",
                "cfr",
            ]
            cmd += _color_args(info, out_h)
        if has_a:
            n_inputs = sum(1 for x in in_args if x == "-i")
            cmd += ["-map", "%d:a:0" % n_inputs, "-c:a", "copy"]
        cmd += [
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            out_path,
        ]
        if dry_run:
            report["passes"].append({"pass": "render", "graph": graph, "cmd": cmd})
            report["dry_run"] = True
            return report
        t1 = time.time()
        core.run(cmd)
        report["passes"].append(
            {"pass": "render", "seconds": round(time.time() - t1, 2)}
        )
    report["seconds_total"] = round(time.time() - t0, 2)
    report["expected_frames"] = plan["total_frames"]
    report["expected_duration"] = plan["expected_duration_ts"]
    report["next"] = (
        "Run edit_verifier.py with this plan and output before calling it done."
    )
    return report
