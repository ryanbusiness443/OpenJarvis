"""Prove a render matches its plan.

Checks: exact frame count, exact frame rate, duration, A/V sync, per-piece
audio cross-correlation against the source (within 1 ms), frame matching at
every cut (best source match must be offset 0 of -2..+2), loudness and
upload spec. Static shots and periodic audio report AMBIGUOUS, not PASS.
"""

from __future__ import annotations

import os
import re
import struct
from fractions import Fraction

from openjarvis.tools.video import core

MATCH_MAX_MAD = 12.0  # best-match thumbnail difference above this = wrong content
AMBIGUITY_MARGIN = 0.75  # best and runner-up closer than this = static shot


def _check(rows, code, ok, detail, blocking=True, status=None):
    rows.append(
        {
            "check": code,
            "status": status or ("PASS" if ok else "FAIL"),
            "blocking": blocking,
            "detail": detail,
        }
    )


def mp4_box_order(path):
    """Top-level MP4 box types in file order (stdlib, reads headers only)."""
    order = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size_total = fh.tell()
            pos = 0
            while pos < size_total and len(order) < 64:
                fh.seek(pos)
                head = fh.read(8)
                if len(head) < 8:
                    break
                size, kind = struct.unpack(">I4s", head)
                if size == 1:
                    size = struct.unpack(">Q", fh.read(8))[0]
                elif size == 0:
                    size = size_total - pos
                order.append(kind.decode("latin-1"))
                if size < 8:
                    break
                pos += size
    except OSError:
        pass
    return order


def measure_loudness(path):
    ffmpeg, _ = core.require_ffmpeg()
    err = core.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
    ).stderr.decode("utf-8", "replace")
    summary = err[err.rfind("Summary:") :]
    out = {}
    for key, pat in (
        ("integrated_lufs", r"I:\s+(-?[\d.]+) LUFS"),
        ("true_peak_dbtp", r"Peak:\s+(-?[\d.]+|-inf) dBFS"),
        ("lra_lu", r"LRA:\s+([\d.]+) LU"),
    ):
        m = re.search(pat, summary)
        if m:
            out[key] = None if m.group(1) == "-inf" else float(m.group(1))
    return out


def _src_vf(plan):
    """Geometry transform the renderer applied, so thumbnails compare alike."""
    from openjarvis.tools.video.planner import PRESETS

    cfg = PRESETS.get(plan["output"].get("preset", "youtube"), {})
    if cfg.get("fill") == "crop" and cfg.get("size"):
        w, h = cfg["size"]
        return "crop='min(iw\\,ih*%d/%d)':'min(ih\\,iw*%d/%d)'" % (w, h, h, w)
    return None


def _grab(path, first, count, fps, vf=None):
    if vf is None:
        return core.grab_frames(path, first, count, fps)
    ffmpeg, _ = core.require_ffmpeg()
    first = max(0, first)
    seek = (Fraction(first) - Fraction(1, 2)) / fps if first else Fraction(0)
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if seek > 0:
        cmd += ["-ss", "%.9f" % float(seek)]
    cmd += [
        "-i",
        path,
        "-map",
        "0:v:0",
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(count),
        "-vf",
        "%s,scale=%d:%d:flags=area,format=gray" % (vf, core.THUMB_W, core.THUMB_H),
        "-f",
        "rawvideo",
        "-",
    ]
    raw = core.run(cmd).stdout
    size = core.THUMB_W * core.THUMB_H
    return [raw[i : i + size] for i in range(0, len(raw) - size + 1, size)]


def _match(out_thumb, src_path, center, fps, vf, src_frames, radius=2):
    """Offset (-radius..radius) of the source frame best matching out_thumb."""
    first = max(0, center - radius)
    last = min(src_frames - 1, center + radius)
    thumbs = _grab(src_path, first, last - first + 1, fps, vf)
    scored = sorted(
        (core.mad(out_thumb, t), first + i - center) for i, t in enumerate(thumbs)
    )
    if not scored:
        return None
    best = scored[0]
    runner = scored[1] if len(scored) > 1 else (255.0, None)
    return {
        "offset": best[1],
        "mad": round(best[0], 2),
        "ambiguous": runner[0] - best[0] < AMBIGUITY_MARGIN,
        "runner_up_mad": round(runner[0], 2),
    }


AUDIO_RATE = 8000  # correlation sample rate: 0.125 ms lag resolution
AUDIO_WINDOW = Fraction(2, 5)
AUDIO_MAX_LAG_MS = 20
AUDIO_TOLERANCE_MS = 1.0


def _pcm(path, start, length, rel_start=Fraction(0)):
    """Mono 8 kHz samples from `start` seconds after the file's first frame."""
    import array

    ffmpeg, _ = core.require_ffmpeg()
    raw = core.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-ss",
            "%.9f" % float(start + rel_start),
            "-i",
            path,
            "-map",
            "0:a:0",
            "-t",
            "%.9f" % float(length),
            "-ac",
            "1",
            "-ar",
            str(AUDIO_RATE),
            "-f",
            "s16le",
            "-",
        ]
    ).stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    return list(samples)


def _best_lag(ref, probe, max_lag):
    """Lag (samples) maximizing normalized cross-correlation of probe against ref."""
    n = min(len(ref), len(probe)) - 2 * max_lag
    if n <= AUDIO_RATE // 20:
        return None, 0.0
    core_ref = ref[max_lag : max_lag + n]
    energy_ref = sum(x * x for x in core_ref) or 1
    scores = {}
    for lag in range(-max_lag, max_lag + 1):
        seg = probe[max_lag + lag : max_lag + lag + n]
        if len(seg) < n:
            continue
        dot = sum(a * b for a, b in zip(core_ref, seg))
        energy = sum(b * b for b in seg) or 1
        scores[lag] = dot / ((energy_ref * energy) ** 0.5)
    if not scores:
        return None, 0.0, False
    best = max(scores, key=scores.get)
    # Periodic audio (a held tone, a hum) correlates almost as well one period
    # away: a rival peak more than 0.5 ms off within 0.05 means no unique answer.
    far = [v for k, v in scores.items() if abs(k - best) * 1000.0 / AUDIO_RATE > 0.5]
    rivals = [
        v
        for k, v in scores.items()
        if abs(k - best) * 1000.0 / AUDIO_RATE > 0.5
        and scores.get(k - 1, -2) <= v >= scores.get(k + 1, -2)
    ]
    periodic = bool(rivals) and max(rivals) > scores[best] - 0.05
    return best, scores[best], periodic and bool(far)


def verify_audio(plan, out_path, max_pieces=12):
    """Cross-correlate source vs output audio inside each sampled piece."""
    fps = core.to_fraction(plan["fps"])
    v_start = core.to_fraction(plan.get("video_start", "0"))
    a_start = core.to_fraction(plan.get("audio_start", "0"))
    segs = [
        s
        for s in plan["segments"]
        if Fraction(s["frames"]) / fps > AUDIO_WINDOW + Fraction(1, 5)
    ]
    if max_pieces and len(segs) > max_pieces:
        step = len(segs) / float(max_pieces)
        segs = [segs[int(i * step)] for i in range(max_pieces)]
    max_lag = AUDIO_RATE * AUDIO_MAX_LAG_MS // 1000
    pad = Fraction(max_lag, AUDIO_RATE)
    rows = []
    for s in segs:
        dur = Fraction(s["frames"]) / fps
        mid = (dur - AUDIO_WINDOW) / 2
        src_t = core.frame_time(s["src_start_frame"], fps) + mid
        out_t = core.frame_time(s["out_start_frame"], fps) + mid
        if src_t + v_start - pad < a_start:
            continue  # window starts before the source audio exists
        ref = _pcm(plan["source"], src_t - pad, AUDIO_WINDOW + 2 * pad)
        got = _pcm(out_path, out_t - pad, AUDIO_WINDOW + 2 * pad)
        rms = (sum(x * x for x in ref) / max(1, len(ref))) ** 0.5
        if rms < 100:  # about -50 dBFS: nothing to align on
            rows.append(
                {
                    "piece": s["index"],
                    "status": "AMBIGUOUS",
                    "detail": "near-silent window",
                }
            )
            continue
        lag, score, periodic = _best_lag(ref, got, max_lag)
        if lag is None:
            rows.append(
                {
                    "piece": s["index"],
                    "status": "AMBIGUOUS",
                    "detail": "window too short",
                }
            )
            continue
        if periodic:
            rows.append(
                {
                    "piece": s["index"],
                    "status": "AMBIGUOUS",
                    "detail": "periodic audio (tone/hum): lag %.3f ms not unique"
                    % (lag * 1000.0 / AUDIO_RATE),
                }
            )
            continue
        lag_ms = lag * 1000.0 / AUDIO_RATE
        ok = abs(lag_ms) <= AUDIO_TOLERANCE_MS and score > 0.5
        rows.append(
            {
                "piece": s["index"],
                "status": "PASS" if ok else "FAIL",
                "lag_ms": round(lag_ms, 3),
                "correlation": round(score, 3),
            }
        )
    return rows


def verify_cuts(plan, out_path, max_cuts=50):
    fps = core.to_fraction(plan["fps"])
    segs = plan["segments"]
    src = plan["source"]
    vf = _src_vf(plan)
    probes = []  # (label, out_frame, src_frame)
    for s in segs:
        probes.append(
            (
                "start of piece %d" % s["index"],
                s["out_start_frame"],
                s["src_start_frame"],
            )
        )
        probes.append(
            (
                "end of piece %d" % s["index"],
                s["out_end_frame"] - 1,
                s["src_end_frame"] - 1,
            )
        )
    if max_cuts and len(probes) > max_cuts * 2:
        step = len(probes) / float(max_cuts * 2)
        probes = [probes[int(i * step)] for i in range(max_cuts * 2)]
    results = []
    for label, of, sf in probes:
        thumbs = core.grab_frames(out_path, of, 1, fps)
        if not thumbs:
            results.append(
                {
                    "where": label,
                    "out_frame": of,
                    "src_frame": sf,
                    "status": "FAIL",
                    "detail": "could not decode output frame",
                }
            )
            continue
        m = _match(thumbs[0], src, sf, fps, vf, plan["source_frames"])
        if m is None:
            status = "FAIL"
        elif m["mad"] > MATCH_MAX_MAD:
            status = "FAIL"
        elif m["ambiguous"]:
            status = "AMBIGUOUS"
        else:
            status = "PASS" if m["offset"] == 0 else "FAIL"
        results.append(
            {
                "where": label,
                "out_frame": of,
                "src_frame": sf,
                "status": status,
                **(m or {}),
            }
        )
    return results


def verify(plan, out_path, max_cuts=50, check_loudness=True):
    if not os.path.isfile(out_path):
        raise core.ToolError("output not found: %s" % out_path)
    info = core.probe(out_path)
    rows = []
    fps = core.to_fraction(plan["fps"])
    frame = 1 / fps
    v, a = info.get("video"), info.get("audio")
    expected = core.to_fraction(plan["expected_duration"])

    if plan["has_video"]:
        if not v:
            _check(rows, "FRAME_COUNT", False, "output has no video stream")
        else:
            counted = core.count_frames(out_path)
            _check(
                rows,
                "FRAME_COUNT",
                counted == plan["total_frames"],
                "counted %s, plan %d" % (counted, plan["total_frames"]),
            )
            out_fps = core.to_fraction(v["fps"])
            avg = core.to_fraction(v["avg_frame_rate"])
            _check(
                rows,
                "FRAME_RATE",
                out_fps == fps and abs(avg - fps) / fps < Fraction(1, 1000),
                "r=%s avg=%s, plan %s" % (v["fps"], v["avg_frame_rate"], plan["fps"]),
            )
            vdur = core.to_fraction(v["duration"])
            _check(
                rows,
                "DURATION",
                abs(vdur - expected) <= frame / 2,
                "video %.6fs, plan %.6fs (delta %.3f ms)"
                % (float(vdur), float(expected), float(vdur - expected) * 1000),
            )
    if plan["has_audio"]:
        if not a:
            _check(rows, "AV_SYNC", False, "output has no audio stream")
        elif v:
            adur = core.to_fraction(a["duration"])
            vdur = core.to_fraction(v["duration"])
            _check(
                rows,
                "AV_SYNC",
                abs(adur - vdur) <= frame,
                "audio %.6fs vs video %.6fs (delta %.3f ms, limit %.3f ms)"
                % (
                    float(adur),
                    float(vdur),
                    float(adur - vdur) * 1000,
                    float(frame) * 1000,
                ),
            )

    audio_rows = []
    if plan["has_audio"] and a and max_cuts != 0:
        audio_rows = verify_audio(plan, out_path)
        fails = [r for r in audio_rows if r["status"] == "FAIL"]
        lags = [abs(r["lag_ms"]) for r in audio_rows if "lag_ms" in r]
        _check(
            rows,
            "AUDIO_ALIGN",
            not fails,
            "%d pieces correlated: worst lag %s ms (limit %.1f), %d ambiguous, %d wrong"
            % (
                len(audio_rows),
                max(lags) if lags else "n/a",
                AUDIO_TOLERANCE_MS,
                sum(1 for r in audio_rows if r["status"] == "AMBIGUOUS"),
                len(fails),
            ),
            status=None if audio_rows else "SKIP",
        )

    cut_rows = []
    if plan["has_video"] and v and max_cuts != 0:
        cut_rows = verify_cuts(plan, out_path, max_cuts)
        fails = [r for r in cut_rows if r["status"] == "FAIL"]
        amb = [r for r in cut_rows if r["status"] == "AMBIGUOUS"]
        _check(
            rows,
            "CUT_FRAMES",
            not fails,
            "%d boundaries checked: %d exact, %d ambiguous (static shot), %d wrong%s"
            % (
                len(cut_rows),
                len(cut_rows) - len(fails) - len(amb),
                len(amb),
                len(fails),
                ""
                if not fails
                else "; first: %s offset %s mad %s"
                % (fails[0]["where"], fails[0].get("offset"), fails[0].get("mad")),
            ),
        )

    if plan["has_audio"] and a and check_loudness and plan["audio"].get("loudness"):
        target = plan["audio"]["loudness"]
        loud = measure_loudness(out_path)
        i_ok = (
            loud.get("integrated_lufs") is not None
            and abs(loud["integrated_lufs"] - float(target.get("target_lufs", -14)))
            <= 1.0
        )
        tp_ok = (
            loud.get("true_peak_dbtp") is None
            or loud["true_peak_dbtp"] <= float(target.get("true_peak_db", -1)) + 0.5
        )
        _check(
            rows,
            "LOUDNESS",
            i_ok and tp_ok,
            "I=%s LUFS (target %s ±1), TP=%s dBTP (ceiling %s +0.5), LRA=%s"
            % (
                loud.get("integrated_lufs"),
                target.get("target_lufs", -14),
                loud.get("true_peak_dbtp"),
                target.get("true_peak_db", -1),
                loud.get("lra_lu"),
            ),
        )

    boxes = mp4_box_order(out_path)
    spec = []
    if v:
        if v["codec"] != "h264":
            spec.append("video codec %s (want h264)" % v["codec"])
        if v["pix_fmt"] != "yuv420p":
            spec.append("pix_fmt %s (want yuv420p)" % v["pix_fmt"])
    if a:
        if a["codec"] != "aac":
            spec.append("audio codec %s (want aac)" % a["codec"])
        if a["sample_rate"] != 48000:
            spec.append("audio %d Hz (want 48000)" % a["sample_rate"])
    if (
        "moov" in boxes
        and "mdat" in boxes
        and boxes.index("moov") > boxes.index("mdat")
    ):
        spec.append("moov after mdat (no +faststart)")
    _check(
        rows,
        "UPLOAD_SPEC",
        not spec,
        "; ".join(spec) or "h264/yuv420p/aac 48k/faststart",
        blocking=False if spec == [] else True,
    )

    failed = [r for r in rows if r["blocking"] and r["status"] == "FAIL"]
    return {
        "verdict": "FAIL" if failed else "PASS",
        "output": os.path.abspath(out_path),
        "checks": rows,
        "cuts": cut_rows,
        "audio": audio_rows,
        "failed": [r["check"] for r in failed],
    }
