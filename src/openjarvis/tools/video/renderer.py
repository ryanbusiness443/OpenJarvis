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
from openjarvis.tools.video.planner import PRESETS


def _video_chain(plan, preset, deinterlace):
    fps = core.to_fraction(plan["fps"])
    parts = []
    if deinterlace:
        parts.append("bwdif=mode=send_frame")
    if plan.get("vfr"):
        parts.append("fps=%s" % plan["fps"])
    terms = "+".join(
        "between(n\\,%d\\,%d)" % (s["src_start_frame"], s["src_end_frame"] - 1)
        for s in plan["segments"]
    )
    parts.append("select='%s'" % terms)
    parts.append("setpts=N/(%d/%d)/TB" % (fps.numerator, fps.denominator))
    cfg = PRESETS[preset]
    if cfg.get("size"):
        w, h = cfg["size"]
        if cfg.get("fill") == "crop":
            parts.append(
                "crop='min(iw\\,ih*%d/%d)':'min(ih\\,iw*%d/%d)'" % (w, h, h, w)
            )
            parts.append("scale=%d:%d:flags=lanczos" % (w, h))
        else:
            parts.append(
                "scale=%d:%d:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2:flags=lanczos" % (w, h)
            )
    parts.append("setsar=1")
    parts.append("format=yuv420p")
    return "[0:v:0]" + ",".join(parts) + "[vout]"


def _audio_chain(plan, loudnorm_filter):
    segs = plan["segments"]
    sr = plan["sample_rate"]
    fade_s = Fraction(plan["audio"].get("crossfade_ms", 12)) / 1000
    lines = []
    k = len(segs)
    if k == 1:
        lines.append("[0:a:0]anull[as0]")
    else:
        lines.append(
            "[0:a:0]asplit=%d%s" % (k, "".join("[as%d]" % i for i in range(k)))
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
        contiguous_prev = i > 0 and segs[i - 1]["src_end_frame"] == s["src_start_frame"]
        contiguous_next = (
            i + 1 < k and segs[i + 1]["src_start_frame"] == s["src_end_frame"]
        )
        if f > 0 and i > 0 and not contiguous_prev:
            chain.append("afade=t=in:d=%.6f" % float(f))
        if f > 0 and i + 1 < k and not contiguous_next:
            chain.append("afade=t=out:st=%.6f:d=%.6f" % (float(dur - f), float(f)))
        # Pin each piece to its exact sample count (pads a short final read).
        chain.append("apad=whole_len=%d" % n)
        chain.append("atrim=end_sample=%d" % n)
        lines.append("[as%d]%s[ab%d]" % (i, ",".join(chain), i))
    tail = "concat=n=%d:v=0:a=1,aresample=48000" % k if k > 1 else "aresample=48000"
    if loudnorm_filter:
        tail += "," + loudnorm_filter + ",aresample=48000"
    # Loudness filters may add or drop a few ms (loudnorm frames, resampler
    # flush); pin the programme to the sample count the video timeline implies.
    fps = core.to_fraction(plan["fps"])
    total = core.frame_of(core.frame_time(plan["total_frames"], fps), 48000)
    tail += ",apad=whole_len=%d,atrim=end_sample=%d" % (total, total)
    lines.append("%s%s[aout]" % ("".join("[ab%d]" % i for i in range(k)), tail))
    return ";\n".join(lines)


def _gain_limiter(cfg, measured):
    """Fallback when linear loudnorm cannot hit the target under the peak ceiling.

    Static gain to the target, then a lookahead limiter (delay-compensated, so
    timing is untouched) at the ceiling minus 1 dB of headroom for inter-sample
    peaks and the AAC encode. The verifier re-measures the result.
    """
    gain = float(cfg.get("target_lufs", -14.0)) - float(measured["input_i"])
    ceiling_db = float(cfg.get("true_peak_db", -1.0)) - 1.0
    return (
        "volume=%.2fdB,alimiter=limit=%.5f:attack=5:release=50:level=disabled:latency=1"
        % (gain, 10 ** (ceiling_db / 20))
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
    report = {"output": os.path.abspath(out_path), "preset": preset, "passes": []}
    t0 = time.time()
    measured = None
    lcfg = plan["audio"].get("loudness") or {}
    with tempfile.TemporaryDirectory() as tmp:
        if has_a and loudness and lcfg:
            graph = _audio_chain(plan, _loudnorm(lcfg))
            gpath = os.path.join(tmp, "measure.txt")
            with open(gpath, "w") as fh:
                fh.write(graph)
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-i",
                src,
                "-filter_complex_script",
                gpath,
                "-map",
                "[aout]",
                "-f",
                "null",
                "-",
            ]
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
        lfilter = None
        if has_a and loudness and lcfg:
            if measured and not _linear_possible(lcfg, measured):
                lfilter = _gain_limiter(lcfg, measured)
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
        graph_parts = []
        if has_v:
            graph_parts.append(_video_chain(plan, preset, deinterlace))
        if has_a:
            graph_parts.append(_audio_chain(plan, lfilter))
        graph = ";\n".join(graph_parts)
        gpath = keep_graph or os.path.join(tmp, "render.txt")
        with open(gpath, "w") as fh:
            fh.write(graph)
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y" if force else "-n",
            "-i",
            src,
            "-filter_complex_script",
            gpath,
        ]
        if has_v:
            gop = max(1, round(fps / 2))
            out_h = (cfg.get("size") or (None, (info["video"] or {}).get("height")))[1]
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
            channels = (info.get("audio") or {}).get("channels") or 2
            kbps = cfg["audio_kbps"] if channels >= 2 else cfg["audio_kbps"] // 2
            cmd += [
                "-map",
                "[aout]",
                "-c:a",
                "aac",
                "-b:a",
                "%dk" % kbps,
                "-ar",
                "48000",
            ]
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
