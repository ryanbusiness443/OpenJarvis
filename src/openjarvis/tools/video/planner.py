"""Compile an EDL into a frame-exact, sample-exact render plan.

Every boundary is snapped to a real frame, no cut may land inside a spoken
word (``MID_WORD``), and audio boundaries derive from the *output* frame
timeline so audio cannot drift from video however many cuts there are.
Verdicts: READY, READY_WITH_WARNINGS, BLOCKED.

EDL: ``{"source", "mode": "cut"|"keep", "segments": [{"start", "end"}],
"words"?, "audio"?, "output": {"preset"}}``. Times accept seconds,
``MM:SS.mmm``, ``HH:MM:SS.mmm``, SMPTE ``HH:MM:SS:FF`` (``;FF`` drop-frame)
or ``"123f"``.
"""

from __future__ import annotations

import os
from fractions import Fraction

from openjarvis.tools.video import core
from openjarvis.tools.video import effects as fx

PRESETS = {
    # name: (max width, max height, crf, x264 preset, audio kbps per stereo pair)
    "youtube": {
        "size": None,
        "crf": 18,
        "x264": "slow",
        "audio_kbps": 384,
        "note": "native resolution and frame rate",
    },
    "youtube-720p": {"size": (1280, 720), "crf": 18, "x264": "slow", "audio_kbps": 384},
    "youtube-1080p": {
        "size": (1920, 1080),
        "crf": 18,
        "x264": "slow",
        "audio_kbps": 384,
    },
    "youtube-1440p": {
        "size": (2560, 1440),
        "crf": 18,
        "x264": "slow",
        "audio_kbps": 384,
    },
    "youtube-4k": {"size": (3840, 2160), "crf": 18, "x264": "slow", "audio_kbps": 384},
    "youtube-shorts": {
        "size": (1080, 1920),
        "crf": 18,
        "x264": "slow",
        "audio_kbps": 384,
        "fill": "crop",
        "max_duration_s": 180,
    },
    "draft": {
        "size": (1280, 720),
        "crf": 28,
        "x264": "veryfast",
        "audio_kbps": 192,
        "note": "fast preview; not for upload",
    },
}


def _finding(findings, severity, code, message, fix=None):
    findings.append(
        {
            "severity": severity,
            "code": code,
            "message": message,
            **({"fix": fix} if fix else {}),
        }
    )


def _word_at(words, t):
    """Word strictly containing t (with a 10 ms tolerance at each edge), or None."""
    tol = Fraction(1, 100)
    for w in words:
        if w["start"] + tol < t < w["end"] - tol:
            return w
        if w["start"] > t:
            break
    return None


def build_plan(
    edl,
    edl_dir=".",
    words_path=None,
    snap_words=False,
    min_keep_frames=6,
    preset_override=None,
    probe_info=None,
):
    findings = []
    src = edl.get("source")
    if not src:
        raise core.ToolError("EDL has no 'source'")
    if not os.path.isabs(src):
        src = os.path.normpath(os.path.join(edl_dir, src))
    info = probe_info or core.probe(src)
    fps = core.fps_of(info)
    video = info.get("video")
    audio = info.get("audio")
    v_start = core.to_fraction(video["start_time"]) if video else Fraction(0)
    duration = (
        core.to_fraction(video["duration"])
        if video
        else core.to_fraction(info["duration"])
    )
    if video and video.get("vfr_suspected"):
        _finding(
            findings,
            "warn",
            "VFR",
            "source is variable frame rate; rendering conforms to %s fps CFR first"
            % video["fps_display"],
            "or re-export the source as CFR",
        )
    if video:
        # Count packets for the exact frame total: a container duration is
        # rounded to microseconds, so duration * fps lands a hair under N.
        counted = core.count_frames(src)
        total_src_frames = counted if counted else core.frame_of(duration, fps)
        if not video.get("vfr_suspected"):
            duration = core.frame_time(total_src_frames, fps)
    else:
        total_src_frames = core.frame_of(duration, fps, "floor")

    mode = edl.get("mode", "keep")
    if mode not in ("keep", "cut"):
        raise core.ToolError("EDL mode must be 'keep' or 'cut', got %r" % mode)

    words = None
    wpath = words_path or edl.get("words")
    if wpath:
        if not os.path.isabs(wpath):
            wpath = os.path.normpath(os.path.join(edl_dir, wpath))
        words = core.load_words(wpath)

    # 1. Parse ranges exactly.
    ranges = []
    for i, seg in enumerate(edl.get("segments", [])):
        try:
            a = core.parse_time(seg["start"], fps)
            b = core.parse_time(seg["end"], fps)
        except KeyError:
            raise core.ToolError("segment %d needs 'start' and 'end'" % i)
        label = seg.get("label") or seg.get("reason") or ""
        if b <= a:
            _finding(
                findings,
                "block",
                "BAD_RANGE",
                "segment %d: end %s <= start %s" % (i, seg["end"], seg["start"]),
            )
            continue
        if a >= duration:
            _finding(
                findings,
                "block",
                "BAD_RANGE",
                "segment %d starts at %s, after the source ends (%s)"
                % (i, core.fmt_ts(a), core.fmt_ts(duration)),
            )
            continue
        if b > duration:
            _finding(
                findings,
                "warn",
                "CLAMPED",
                "segment %d end %s clamped to source end %s"
                % (i, core.fmt_ts(b), core.fmt_ts(duration)),
            )
            b = duration
        ranges.append(
            {
                "a": a,
                "b": b,
                "label": label,
                "i": i,
                "transition": seg.get("transition"),
            }
        )

    if mode == "keep" and [r["i"] for r in ranges] != [
        r["i"] for r in sorted(ranges, key=lambda r: r["a"])
    ]:
        if not edl.get("allow_reorder"):
            _finding(
                findings,
                "warn",
                "REORDERED",
                "keep ranges were out of source order and were sorted",
                'set "allow_reorder": true to rearrange content deliberately',
            )
    if not (mode == "keep" and edl.get("allow_reorder")):
        ranges.sort(key=lambda r: r["a"])
        merged = []
        for r in ranges:
            if merged and r["a"] <= merged[-1]["b"]:
                if r["a"] < merged[-1]["b"]:
                    _finding(
                        findings,
                        "warn",
                        "OVERLAP",
                        "segments %d and %d overlap; merged"
                        % (merged[-1]["i"], r["i"]),
                    )
                merged[-1]["b"] = max(merged[-1]["b"], r["b"])
                merged[-1]["label"] = merged[-1]["label"] or r["label"]
            else:
                merged.append(dict(r))
        ranges = merged

    # 2. Convert cut mode into keep ranges.
    if mode == "cut":
        # A cut's "transition" applies to the join it creates, i.e. into the
        # keep that follows it.
        keeps, cursor, pending = [], Fraction(0), None
        for r in ranges:
            if r["a"] - cursor > 0:
                keeps.append(
                    {"a": cursor, "b": r["a"], "label": "", "transition": pending}
                )
            cursor, pending = r["b"], r.get("transition")
        if duration - cursor > 0:
            keeps.append(
                {"a": cursor, "b": duration, "label": "", "transition": pending}
            )
        cut_lengths = [r["b"] - r["a"] for r in ranges]
        for r, length in zip(ranges, cut_lengths):
            if length * fps < 3:
                _finding(
                    findings,
                    "warn",
                    "MICRO_CUT",
                    "cut at %s removes only %.1f frames — a visible jump for no gain"
                    % (core.fmt_ts(r["a"]), float(length * fps)),
                    "delete this cut",
                )
        ranges = keeps

    # 3. Word-boundary guard, then snap to frames.
    max_snap = Fraction(0)
    segments = []
    for r in ranges:
        a_mode = b_mode = "nearest"
        if words:
            for edge in ("a", "b"):
                t = r[edge]
                w = _word_at(words, t)
                if not w:
                    continue
                inside_keep = (w["end"] - t) if edge == "a" else (t - w["start"])
                keep_word = inside_keep * 2 >= (w["end"] - w["start"])
                target = (
                    (w["start"] if keep_word else w["end"])
                    if edge == "a"
                    else (w["end"] if keep_word else w["start"])
                )
                if snap_words:
                    r[edge] = target
                    # include word -> snap outward; exclude word -> snap inward
                    outward = keep_word
                    if edge == "a":
                        a_mode = "floor" if outward else "ceil"
                    else:
                        b_mode = "ceil" if outward else "floor"
                    _finding(
                        findings,
                        "info",
                        "MID_WORD_FIXED",
                        "%s edge at %s was inside %r; moved to %s (%s the word)"
                        % (
                            "start" if edge == "a" else "end",
                            core.fmt_ts(t),
                            w["word"],
                            core.fmt_ts(target),
                            "keeping" if keep_word else "dropping",
                        ),
                    )
                else:
                    _finding(
                        findings,
                        "warn",
                        "MID_WORD",
                        "%s edge at %s falls inside the word %r (%s-%s)"
                        % (
                            "start" if edge == "a" else "end",
                            core.fmt_ts(t),
                            w["word"],
                            core.fmt_ts(w["start"]),
                            core.fmt_ts(w["end"]),
                        ),
                        "move it to %s, or rerun with --snap-words"
                        % core.fmt_ts(target),
                    )
        fa = core.frame_of(r["a"], fps, a_mode)
        fb = core.frame_of(r["b"], fps, b_mode)
        fb = min(fb, total_src_frames)
        max_snap = max(
            max_snap,
            abs(core.frame_time(fa, fps) - r["a"]),
            abs(core.frame_time(fb, fps) - r["b"]),
        )
        if fb <= fa:
            # Shorter than a frame: zero-length keeps are dropped (in cut mode
            # they are slivers between two cuts), reported as a block in keep mode.
            if mode == "keep":
                _finding(
                    findings,
                    "block",
                    "ZERO_FRAMES",
                    "keep range %s-%s is shorter than one frame"
                    % (core.fmt_ts(r["a"]), core.fmt_ts(r["b"])),
                )
            continue
        segments.append(
            {
                "src_start_frame": fa,
                "src_end_frame": fb,
                "label": r["label"],
                "trans_req": r.get("transition"),
            }
        )

    # Merge keeps that became contiguous after snapping (no cut between them).
    joined = []
    for s in segments:
        if (
            joined
            and s["src_start_frame"] == joined[-1]["src_end_frame"]
            and not edl.get("allow_reorder")
        ):
            joined[-1]["src_end_frame"] = s["src_end_frame"]
        else:
            joined.append(s)
    segments = joined

    if not segments:
        _finding(findings, "block", "EMPTY_RESULT", "the edit leaves nothing to render")

    # 4. Output timeline (transitions overlap pieces) + sample-exact audio
    #    boundaries derived from it.
    sr = audio["sample_rate"] if audio else 0
    a_start = core.to_fraction(audio["start_time"]) if audio else Fraction(0)
    for idx, s in enumerate(segments):
        s["index"] = idx
    total_frames = fx.apply_transitions(segments, edl, fps, findings)
    for idx, s in enumerate(segments):
        n = s["frames"]
        out_frame = s["out_start_frame"]
        s["src_start"] = core.fmt_ts(core.frame_time(s["src_start_frame"], fps), 6)
        s["src_end"] = core.fmt_ts(core.frame_time(s["src_end_frame"], fps), 6)
        s["out_start"] = core.fmt_ts(core.frame_time(out_frame, fps), 6)
        s["duration_s"] = core.fsec(core.frame_time(n, fps), 6)
        if sr:
            out_s0 = core.frame_of(
                core.frame_time(out_frame, fps), sr
            )  # nearest sample
            out_s1 = core.frame_of(core.frame_time(out_frame + n, fps), sr)
            count = out_s1 - out_s0
            src_t = v_start + core.frame_time(s["src_start_frame"], fps) - a_start
            # Negative when audio starts after the video: the renderer pads
            # that many samples of silence instead of shifting the audio early.
            s0 = core.frame_of(src_t, sr)
            s["audio_start_sample"] = s0
            s["audio_end_sample"] = s0 + count
        if n < min_keep_frames and len(segments) > 1:
            _finding(
                findings,
                "warn",
                "MICRO_KEEP",
                "kept piece %d (%s) is only %d frames — reads as a flash frame"
                % (idx, s["src_start"], n),
                "extend it or cut it",
            )
    out_frame = total_frames

    if max_snap:
        _finding(
            findings,
            "info",
            "SNAPPED",
            "largest boundary snap: %.3f ms (nearest-snap bound is half a frame, "
            "%.3f ms; word-safe snaps move outward up to one frame)"
            % (float(max_snap) * 1000, float(Fraction(1, 2) / fps) * 1000),
        )

    out_cfg = dict(edl.get("output") or {})
    preset = preset_override or out_cfg.get("preset", "youtube")
    if preset not in PRESETS:
        raise core.ToolError(
            "unknown output preset %r; choose from %s"
            % (preset, ", ".join(sorted(PRESETS)))
        )
    out_cfg["preset"] = preset
    total_dur = core.frame_time(out_frame, fps)
    max_d = PRESETS[preset].get("max_duration_s")
    if max_d and total_dur > max_d:
        _finding(
            findings,
            "block",
            "TOO_LONG",
            "%s output must be <= %ss; this edit is %.2fs"
            % (preset, max_d, float(total_dur)),
        )

    effects = fx.plan_effects(edl, segments, out_frame, fps, edl_dir, findings)

    audio_cfg = dict(edl.get("audio") or {})
    audio_cfg.setdefault("crossfade_ms", 12)
    audio_cfg.setdefault(
        "loudness", {"target_lufs": -14.0, "true_peak_db": -1.0, "lra": 11.0}
    )

    sev = {f["severity"] for f in findings}
    verdict = (
        "BLOCKED"
        if "block" in sev
        else ("READY_WITH_WARNINGS" if "warn" in sev else "READY")
    )
    removed = duration - total_dur
    return {
        "plan_version": 1,
        "verdict": verdict,
        "source": info["path"],
        "fps": core.frac_str(fps),
        "fps_display": core.fsec(fps, 3),
        "vfr": bool(video and video.get("vfr_suspected")),
        "has_video": bool(video),
        "has_audio": bool(audio),
        "sample_rate": sr,
        "video_start": core.frac_str(v_start),
        "audio_start": core.frac_str(a_start),
        "source_duration": core.frac_str(duration),
        "source_frames": total_src_frames,
        "segments": segments,
        "total_frames": out_frame,
        "expected_duration": core.frac_str(total_dur),
        "expected_duration_ts": core.fmt_ts(total_dur, 6),
        "removed_s": core.fsec(removed, 3),
        "audio": audio_cfg,
        "output": out_cfg,
        "words": wpath and os.path.abspath(wpath),
        "transition_default": edl.get("transition") or "cut",
        "effects": effects,
        "findings": findings,
    }
