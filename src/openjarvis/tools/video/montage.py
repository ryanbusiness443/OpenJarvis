"""Cut a pile of clips to the music: every cut on a beat.

Ported from claude-skills marketing/youtube-editor (montage_planner.py).
"""

from __future__ import annotations

import os
from fractions import Fraction

from openjarvis.tools.video import core

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts")
STYLES = {
    "day-in-life": {
        "beats": 2,
        "rise_beats": 2,
        "grade": "warm-vlog",
        "look": {"grain": 3, "vignette": 0.2},
    },
    "motivation": {
        "beats": 2,
        "rise_beats": 1,
        "grade": "teal-orange",
        "look": {"grain": 6, "vignette": 0.35},
        "letterbox_long": 2.0,
    },
    "storytelling": {
        "beats": 4,
        "rise_beats": 2,
        "grade": "moody",
        "look": {"grain": 8, "vignette": 0.45},
    },
}
FORMATS = {"short": ((1080, 1920), "youtube-shorts"), "long": ((1920, 1080), "youtube")}


def find_clips(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(
                os.path.join(p, f)
                for f in os.listdir(p)
                if f.lower().endswith(VIDEO_EXT)
            )
        elif os.path.isfile(p):
            out.append(p)
        else:
            raise core.ToolError("not found: %s" % p)
    if len(out) < 2:
        raise core.ToolError("a montage needs at least 2 clips")
    return out


def bind_clips(clips, out_path, size, fps, ambient_db=-30.0):
    """Conform + join; returns the index [{file, start, frames, duration, label}]."""
    ffmpeg, _ = core.require_ffmpeg()
    W, H = size
    inputs, graph, index, pos = [], [], [], 0
    for i, c in enumerate(clips):
        info = core.probe(c)
        dur = core.to_fraction(info["duration"])
        frames = int(dur * fps) - 1  # one frame short of the end: no partial frame
        if frames < int(fps):
            raise core.ToolError("clip shorter than a second: %s" % c)
        inputs += ["-i", c]
        graph.append(
            "[%d:v:0]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
            "setsar=1,fps=%s,trim=end_frame=%d,setpts=PTS-STARTPTS,format=yuv420p[v%d]"
            % (i, W, H, W, H, core.frac_str(fps), frames, i)
        )
        secs = core.fsec(Fraction(frames) / fps, 6)
        if info.get("audio") and ambient_db is not None:
            graph.append(
                "[%d:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                "volume=%.1fdB,apad,atrim=end=%s,asetpts=PTS-STARTPTS[a%d]"
                % (i, ambient_db, secs, i)
            )
        else:
            graph.append("anullsrc=r=48000:cl=stereo,atrim=end=%s[a%d]" % (secs, i))
        index.append(
            {
                "file": os.path.abspath(c),
                "label": os.path.splitext(os.path.basename(c))[0],
                "start_frame": pos,
                "frames": frames,
                "start": core.frac_str(Fraction(pos) / fps),
                "creation_time": (info.get("tags") or {}).get("creation_time"),
            }
        )
        pos += frames
    graph.append(
        "".join("[v%d][a%d]" % (i, i) for i in range(len(clips)))
        + "concat=n=%d:v=1:a=1[v][a]" % len(clips)
    )
    core.run(
        [ffmpeg, "-v", "error", "-y", "-nostdin"]
        + inputs
        + [
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "14",
            "-g",
            str(int(fps)),
            "-r",
            core.frac_str(fps),
            "-c:a",
            "pcm_s16le" if out_path.endswith(".mov") else "aac",
            "-b:a",
            "320k",
            "-movflags",
            "+faststart",
            out_path,
        ]
    )
    return index


def _clock(ts):
    """'2026-10-31T10:48:48.000000Z' -> '10:48 AM' (the camera's clock, as recorded)."""
    try:
        hh, mm = int(ts[11:13]), int(ts[14:16])
    except (TypeError, ValueError):
        return None
    return "%d:%02d %s" % (hh % 12 or 12, mm, "AM" if hh < 12 else "PM")


def shot_grid(beats, length, style, sections):
    """Cut times (seconds) on beats: shots of N beats, fewer in rises."""
    cfg = STYLES[style]
    rises = []
    for i, sct in enumerate(sections):
        if sct.get("change") == "rise":
            end = next(
                (x["start"] for x in sections[i + 1 :] if x.get("change") == "drop"),
                1e9,
            )
            rises.append((sct["start"], end))
    cuts, k = [], 0
    usable = [b for b in beats if 0 < b < length - 0.5]
    while k < len(usable):
        t = usable[k]
        if not cuts or t - cuts[-1] > 0.3:
            cuts.append(t)
        in_rise = any(a <= t < b for a, b in rises)
        k += cfg["rise_beats"] if in_rise else cfg["beats"]
    return cuts, rises


def plan_montage(
    index,
    bound,
    music,
    beats_rep,
    style="day-in-life",
    fmt="short",
    length=None,
    labels=None,
    texts=(),
    fps=Fraction(30),
):
    cfg = STYLES[style]
    notes, effects, segs = [], [], []
    beats = beats_rep["beats"]
    downbeats = set(round(b, 4) for b in beats_rep.get("downbeats", []))
    total_src = sum(c["frames"] for c in index) / float(fps)
    music_len = core.to_fraction(core.probe(music)["duration"])
    length = float(length or min(float(music_len), 60.0 if fmt == "short" else 180.0))
    length = min(length, float(music_len))
    cuts, rises = shot_grid(beats, length, style, beats_rep.get("sections", []))
    # the edit ends on the last beat at or before the length (a clean ending)
    end = max([b for b in beats if b <= length] or [length])
    cuts = [c for c in cuts if c < end - 0.3]
    bounds = [0.0] + cuts + [end]
    notes.append(
        "Music: %s at %.1f BPM (confidence %.2f); %d shots on the beat over %.1fs."
        % (
            os.path.basename(music),
            beats_rep["tempo_bpm"],
            beats_rep["confidence"],
            len(bounds) - 1,
            end,
        )
    )
    if beats_rep["confidence"] < 0.3:
        notes.append(
            "Warning: the beat is weak or irregular; cuts follow the detected grid, "
            "check by ear (or pass the tempo: beat_detector.py --bpm)."
        )
    # assign shots: clips in order, round-robin; each shot from the middle of the
    # clip's unused material, never reusing a frame
    cursor = {i: 0.2 for i in range(len(index))}
    order = list(range(len(index)))
    shot_clip = []
    reused = 0
    # quantise in frames once: cut k lands on frame round(beat_k * fps), and each
    # shot is exactly the frames between two cuts, so nothing drifts
    fb = [int(round(Fraction(str(x)) * fps)) for x in bounds]
    for n, (a, b) in enumerate(zip(bounds, bounds[1:])):
        nfr = fb[n + 1] - fb[n]
        need = nfr / float(fps)
        for tries in range(len(order)):
            ci = order[(n + tries) % len(order)]
            c = index[ci]
            clen = c["frames"] / float(fps)
            start = cursor[ci]
            if n < len(order) and clen > need + 0.4:
                start = max(
                    start, (clen - need) / 2.0 - need
                )  # first visit: near the middle
            if start + need <= clen - 0.1:
                break
        else:
            # every clip is used up at this point: reuse the longest one from the top
            # (editors do), and say so
            ci = max(order, key=lambda i: index[i]["frames"])
            c = index[ci]
            clen = c["frames"] / float(fps)
            if need > clen - 0.3:
                raise core.ToolError(
                    "a %.2fs shot is longer than every clip; slow the pace "
                    "(--style storytelling) or add longer clips" % need
                )
            start = 0.2
            reused += 1
        cursor[ci] = start + need + 0.05
        f0 = c["start_frame"] + int(start * float(fps))
        segs.append(
            {
                "start": "%df" % f0,
                "end": "%df" % (f0 + nfr),
                "label": "shot %d: %s (beat %.3fs)" % (n + 1, c["label"], a),
            }
        )
        shot_clip.append(ci)
    if reused:
        notes.append(
            "Footage: %d shots reuse material already shown (%.1fs of clips for a "
            "%.1fs edit). More clips, or a shorter --length, avoid repeats."
            % (reused, total_src, end)
        )
    # the bound file is in clip order; shots revisit clips, so keep order is deliberate
    effects.append(
        {
            "type": "music",
            "file": os.path.abspath(music),
            "volume_db": -3,
            "duck": False,
            "fade_in": 0.0,
            "fade_out": 1.5 if style != "motivation" else 0.6,
        }
    )
    seen = set()
    for n, (a, ci) in enumerate(zip(bounds, shot_clip)):
        is_down = round(a, 4) in downbeats
        if style == "day-in-life" and ci not in seen:
            seen.add(ci)
            lab = (
                labels[ci]
                if labels and ci < len(labels)
                else _clock(index[ci].get("creation_time"))
            )
            if lab:
                effects.append(
                    {
                        "type": "text",
                        "start": "%.4f" % a,
                        "end": "%.4f" % min(a + 1.6, bounds[n + 1]),
                        "text": lab.upper(),
                        "position": "top",
                        "style": "box",
                        "size": 0.035,
                        "time": "output",
                    }
                )
            if n:
                effects.append(
                    {
                        "type": "sfx",
                        "sound": "camera",
                        "at": "%.4f" % a,
                        "time": "output",
                    }
                )
        if (
            style == "motivation"
            and n
            and is_down
            and any(r0 <= a < r1 for r0, r1 in rises)
        ):
            effects.append(
                {
                    "type": "flash",
                    "start": "%.4f" % a,
                    "end": "%.4f" % (a + 0.15),
                    "peak": 0.55,
                    "time": "output",
                }
            )
            effects.append(
                {"type": "sfx", "sound": "boom", "at": "%.4f" % a, "time": "output"}
            )
        if style == "storytelling" and n % 2 == 0:
            effects.append(
                {
                    "type": "zoom",
                    "start": "%.4f" % a,
                    "end": "%.4f" % bounds[n + 1],
                    "scale": 1.08,
                    "center": [0.5, 0.5],
                    "time": "output",
                }
            )
    if style == "motivation" and rises:
        r0 = next((b for b in beats if b >= rises[0][0]), None)
        if r0 is not None and r0 < end - 1:
            effects.append(
                {
                    "type": "shake",
                    "start": "%.4f" % r0,
                    "end": "%.4f" % (r0 + 0.35),
                    "intensity": 0.012,
                    "time": "output",
                }
            )
            notes.append(
                "Motivation: shake + flashes where the music rises (%.1fs)." % r0
            )
    if texts:
        downs = [b for b in sorted(downbeats) if 1.0 <= b < end - 2.0] or bounds[1:-1]
        step = max(1, len(downs) // (len(texts) + 1))
        placed = 0
        for j, line in enumerate(texts):
            k = (j + 1) * step
            if k >= len(downs):
                break
            a = downs[k]
            b = next((x for x in downs if x > a + 1.5), a + 2.0)
            effects.append(
                {
                    "type": "text",
                    "start": "%.4f" % a,
                    "end": "%.4f" % min(b, end),
                    "text": line.upper(),
                    "time": "output",
                    "position": "center" if style == "motivation" else "bottom",
                    "style": "hook" if style == "motivation" else "outline",
                    "size": 0.05 if fmt == "short" else 0.07,
                }
            )
            placed += 1
        notes.append(
            "Text: %d of %d lines on downbeats%s."
            % (
                placed,
                len(texts),
                ""
                if placed == len(texts)
                else " (the rest did not fit; lengthen the edit or cut lines)",
            )
        )
    look = dict(cfg["look"])
    if fmt == "long" and cfg.get("letterbox_long"):
        look["letterbox"] = cfg["letterbox_long"]
    effects.append(dict({"type": "look"}, **look))
    if style == "storytelling":
        effects.append({"type": "fade_out", "duration": 1.0})
    edl = {
        "source": os.path.abspath(bound),
        "mode": "keep",
        "allow_reorder": True,
        "segments": segs,
        "transition": {"type": "cut"},
        "effects": effects,
        "output": {"preset": FORMATS[fmt][1]},
        "audio": {
            "crossfade_ms": 4,
            "loudness": {"target_lufs": -14.0, "true_peak_db": -1.0, "lra": 11.0},
        },
        "beat_sync": {
            "music": os.path.abspath(music),
            "music_start": 0,
            "beats": [b for b in beats if b <= end + 1],
            "all_cuts_on_beat": True,
        },
        "montage": {"style": style, "format": fmt, "clips": index},
    }
    return edl, notes


def run(
    clips,
    music,
    style,
    fmt,
    bound_out,
    length=None,
    labels=None,
    texts=(),
    beats=None,
    bpm=None,
    pack=None,
    pack_dir=None,
    ambient_db=-30.0,
    fps=Fraction(30),
):
    from openjarvis.tools.video import beats as beat_detector
    from openjarvis.tools.video import packs as asset_packs
    from openjarvis.tools.video.viral import apply_pack

    size = FORMATS[fmt][0]
    index = bind_clips(clips, bound_out, size, fps, ambient_db)
    rep = core.load_json(beats) if beats else beat_detector.detect(music, bpm=bpm)
    edl, notes = plan_montage(
        index, bound_out, music, rep, style, fmt, length, labels, texts, fps
    )
    if pack is not None:
        pack = asset_packs.load_pack(pack)
    apply_pack(
        edl["effects"],
        style,
        pack,
        pack_dir
        or os.path.join(os.path.dirname(os.path.abspath(bound_out)), "pack-" + style),
        notes,
    )
    return edl, notes
