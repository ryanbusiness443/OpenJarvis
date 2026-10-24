"""Measure a source video and propose a cut list (read-only on the source).

``analyze`` finds silences, black segments, scene changes and EBU R128
loudness in one decode pass. ``propose`` turns silences (and, given word
timestamps, filler words) into an editable cut-mode EDL with padding, never
trimming inside a word. Proposals are for review, not for direct rendering.
"""

from __future__ import annotations

import os
import re
from fractions import Fraction

from openjarvis.tools.video import core

# Disfluencies that are almost never content. "like", "so", "you know" are
# deliberately absent: they carry meaning too often to cut without listening.
FILLERS = {"um", "umm", "uh", "uhh", "erm", "er", "ah", "ahh", "hmm", "mm", "mhm", "eh"}


def _num(text):
    return core.to_fraction(text)


def analyze(path, noise_db=-35.0, min_silence=0.5, scene=0.35, black=0.5):
    info = core.probe(path)
    ffmpeg, _ = core.require_ffmpeg()
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-i", path]
    if info["video"]:
        cmd += [
            "-map",
            "0:v:0",
            "-vf",
            "scale=320:-2,blackdetect=d=%s:pix_th=0.10,select='gt(scene\\,%s)',showinfo"
            % (black, scene),
        ]
    if info["audio"]:
        cmd += [
            "-map",
            "0:a:0",
            "-af",
            "silencedetect=n=%sdB:d=%s,ebur128=peak=true" % (noise_db, min_silence),
        ]
    cmd += ["-f", "null", "-"]
    err = core.run(cmd).stderr.decode("utf-8", "replace")

    silences, start = [], None
    for line in err.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(Fraction(0), _num(m.group(1)))
            continue
        m = re.search(r"silence_end: (-?[\d.]+)", line)
        if m and start is not None:
            silences.append((start, _num(m.group(1))))
            start = None
    if start is not None:  # silence runs to end of file
        silences.append((start, core.to_fraction(info["duration"])))

    blacks = [
        (_num(a), _num(b))
        for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", err)
    ]
    scenes = [
        _num(t) for t in re.findall(r"\[Parsed_showinfo.*?pts_time:([\d.]+)", err)
    ]

    loud = {}
    summary = err[err.rfind("Summary:") :] if "Summary:" in err else ""
    for key, pat in (
        ("integrated_lufs", r"I:\s+(-?[\d.]+) LUFS"),
        ("lra_lu", r"LRA:\s+([\d.]+) LU"),
        ("true_peak_dbtp", r"Peak:\s+(-?[\d.]+|-inf) dBFS"),
    ):
        m = re.search(pat, summary)
        if m:
            loud[key] = None if m.group(1) == "-inf" else float(m.group(1))

    kfs = core.keyframes(path) if info["video"] else []
    gaps = [b - a for a, b in zip(kfs, kfs[1:])]
    return {
        "source": info["path"],
        "probe": info,
        "silences": [
            {
                "start": core.fmt_ts(a, 6),
                "end": core.fmt_ts(b, 6),
                "duration": core.fsec(b - a, 3),
            }
            for a, b in silences
        ],
        "black_segments": [
            {"start": core.fmt_ts(a, 6), "end": core.fmt_ts(b, 6)} for a, b in blacks
        ],
        "scene_changes": [core.fmt_ts(t, 6) for t in scenes],
        "loudness": loud,
        "keyframes": {
            "count": len(kfs),
            "max_gap_s": core.fsec(max(gaps), 3) if gaps else None,
            "mean_gap_s": core.fsec(sum(gaps, Fraction(0)) / len(gaps), 3)
            if gaps
            else None,
            "note": "A stream-copy cut can only start on a keyframe, so it can land "
            "up to max_gap_s early. The renderer re-encodes to cut on the exact frame.",
        },
        "params": {
            "noise_db": noise_db,
            "min_silence": min_silence,
            "scene_threshold": scene,
            "black_min": black,
        },
    }


def _overlapping_words(words, a, b):
    return [w for w in words if w["start"] < b and w["end"] > a]


def propose(
    path,
    words=None,
    noise_db=-35.0,
    min_silence=0.6,
    pad=0.15,
    min_cut=0.25,
    fillers=True,
):
    """Build a cut-mode EDL from silences (and filler words)."""
    result = analyze(path, noise_db=noise_db, min_silence=min_silence)
    duration = core.to_fraction(result["probe"]["duration"])
    pad_f, min_cut_f = core.to_fraction(pad), core.to_fraction(min_cut)
    cuts, notes = [], []

    for s in result["silences"]:
        a = core.parse_time(s["start"])
        b = core.parse_time(s["end"])
        # Keep `pad` of room at each edge, but never pad past the file edges.
        lo = a + pad_f if a > 0 else a
        hi = b - pad_f if b < duration else b
        if words:
            for w in _overlapping_words(words, lo, hi):
                # silencedetect mistook a quiet word for silence: shrink around it.
                if w["start"] - lo > hi - w["end"]:
                    hi = min(hi, w["start"] - pad_f)
                else:
                    lo = max(lo, w["end"] + pad_f)
            if _overlapping_words(words, lo, hi):
                notes.append("skipped silence at %s: words inside it" % s["start"])
                continue
        if hi - lo >= min_cut_f:
            cuts.append(
                {
                    "start": lo,
                    "end": hi,
                    "kind": "silence",
                    "reason": "silence %.2fs (padded %.2fs each side)"
                    % (s["duration"], pad),
                }
            )

    if words and fillers:
        for i, w in enumerate(words):
            token = re.sub(r"[^a-z]", "", w["word"].lower())
            if token not in FILLERS:
                continue
            prev_end = words[i - 1]["end"] if i else Fraction(0)
            next_start = words[i + 1]["start"] if i + 1 < len(words) else duration
            lo = max(prev_end, w["start"])
            # Take the pause after the filler too, leaving a natural 60 ms beat.
            hi = max(w["end"], next_start - Fraction(6, 100))
            cuts.append(
                {
                    "start": lo,
                    "end": hi,
                    "kind": "filler",
                    "reason": "filler word %r" % w["word"],
                }
            )

    cuts.sort(key=lambda c: c["start"])
    merged = []
    for c in cuts:
        if merged and c["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], c["end"])
            merged[-1]["reason"] += " + " + c["reason"]
            merged[-1]["kind"] = (
                "mixed" if merged[-1]["kind"] != c["kind"] else c["kind"]
            )
        else:
            merged.append(dict(c))
    removed = sum((c["end"] - c["start"] for c in merged), Fraction(0))
    edl = {
        "version": 1,
        "source": result["source"],
        "mode": "cut",
        "segments": [
            {
                "start": core.fmt_ts(c["start"], 6),
                "end": core.fmt_ts(c["end"], 6),
                "kind": c["kind"],
                "reason": c["reason"],
            }
            for c in merged
        ],
        "audio": {
            "crossfade_ms": 12,
            "loudness": {"target_lufs": -14.0, "true_peak_db": -1.0, "lra": 11.0},
        },
        "output": {"preset": "youtube-1080p"},
        "summary": {
            "cuts": len(merged),
            "removed_s": core.fsec(removed, 3),
            "source_s": core.fsec(duration, 3),
            "result_s": core.fsec(duration - removed, 3),
        },
        "notes": notes,
        "review": "PROPOSAL ONLY. Read every cut before planning; delete any that "
        "remove a deliberate pause, a punchline beat, or content.",
    }
    if words:
        edl["words_used"] = True
    return edl, result


def make_sample(tmpdir):
    """Synthetic talking-head stand-in: frame-counter video + speech-like audio.

    The audio is noise shaped into ~3 syllables a second (aperiodic, so the
    verifier's cross-correlation has one unique answer) with two silent gaps.
    """
    ffmpeg, _ = core.require_ffmpeg()
    path = os.path.join(tmpdir, "sample.mp4")
    audio = (
        "aevalsrc='0.5*(random(0)*2-1)*(0.55+0.45*sin(2*PI*3.1*t))"
        "*(1-between(t,2,3.2))*(1-between(t,5,5.9))':s=48000:d=8"
    )
    core.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30000/1001:duration=8",
            "-f",
            "lavfi",
            "-i",
            audio,
            "-c:v",
            "libx264",
            "-g",
            "60",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            path,
        ]
    )
    return path
