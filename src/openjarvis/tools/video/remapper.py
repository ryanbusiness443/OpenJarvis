"""Carry chapters, captions and timestamps through an edit.

Chapters are remapped and checked against YouTube's rules (first at 0:00, at
least 3, each at least 10 s); caption cues crossing a cut are clipped and
re-joined; with word timestamps, captions are rebuilt from kept words only.
"""

from __future__ import annotations

import bisect
import re
from fractions import Fraction

from openjarvis.tools.video import core

MIN_CHAPTER_S = 10
MIN_CHAPTERS = 3


class Timeline:
    def __init__(self, plan):
        self.fps = core.to_fraction(plan["fps"])
        self.pieces = []
        for s in plan["segments"]:
            a = core.frame_time(s["src_start_frame"], self.fps)
            b = core.frame_time(s["src_end_frame"], self.fps)
            o = core.frame_time(s["out_start_frame"], self.fps)
            self.pieces.append((a, b, o))
        self.ordered = sorted(self.pieces)
        self.starts = [p[0] for p in self.ordered]
        self.total = core.to_fraction(plan["expected_duration"])

    def map(self, t):
        """Source time -> output time, or None if t falls in removed material."""
        i = bisect.bisect_right(self.starts, t) - 1
        if i >= 0:
            a, b, o = self.ordered[i]
            if a <= t < b:
                return o + (t - a)
        return None

    def next_kept(self, t):
        """Output time of the first kept material at or after source time t."""
        i = bisect.bisect_left(self.starts, t)
        if i < len(self.ordered):
            return self.ordered[i][2]
        return None

    def map_range(self, a, b):
        """Source range -> list of output ranges (in output order)."""
        out = []
        for pa, pb, po in self.pieces:
            lo, hi = max(a, pa), min(b, pb)
            if hi > lo:
                out.append((po + (lo - pa), po + (hi - pa)))
        out.sort()
        return out


# -------------------------------------------------------------------- chapters

_CH_RE = re.compile(
    r"^\s*\(?((?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?)\)?\s*[-–—:|]?\s*(.+?)\s*$"
)


def load_chapters(path):
    if path.endswith(".json"):
        data = core.load_json(path)
        items = data.get("chapters", data) if isinstance(data, dict) else data
        return [
            (core.parse_time(c.get("time", c.get("start"))), c["title"]) for c in items
        ]
    chapters = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = _CH_RE.match(line)
            if m:
                chapters.append((core.parse_time(m.group(1)), m.group(2)))
    if not chapters:
        raise core.ToolError(
            "no chapters found in %s (want lines like '1:23 Title')" % path
        )
    return chapters


def remap_chapters(plan, chapters):
    tl = Timeline(plan)
    findings, mapped = [], []
    for t, title in sorted(chapters):
        o = tl.map(t)
        if o is None:
            o = tl.next_kept(t)
            if o is None:
                findings.append(
                    {
                        "code": "DROPPED",
                        "message": "%r at %s is after the last kept "
                        "frame" % (title, core.fmt_ts(t)),
                    }
                )
                continue
            findings.append(
                {
                    "code": "MOVED",
                    "message": "%r at %s was inside a cut; moved to the "
                    "next kept frame (%s)"
                    % (title, core.fmt_ts(t), core.fmt_chapter(o)),
                }
            )
        mapped.append([o, title])
    # Same output second -> keep the later title (it describes what follows).
    dedup = []
    for o, title in mapped:
        if dedup and core.fmt_chapter(dedup[-1][0]) == core.fmt_chapter(o):
            findings.append(
                {
                    "code": "COLLAPSED",
                    "message": "%r replaced %r at the same second"
                    % (title, dedup[-1][1]),
                }
            )
            dedup[-1] = [o, title]
        else:
            dedup.append([o, title])
    if dedup and core.fmt_chapter(dedup[0][0]) != "0:00":
        findings.append(
            {
                "code": "FIRST_NOT_ZERO",
                "message": "first chapter %r starts at %s; "
                "YouTube requires 0:00, so it was moved there"
                % (dedup[0][1], core.fmt_chapter(dedup[0][0])),
            }
        )
        dedup[0][0] = Fraction(0)
    kept = []
    for i, (o, title) in enumerate(dedup):
        end = dedup[i + 1][0] if i + 1 < len(dedup) else tl.total
        if end - o < MIN_CHAPTER_S and kept and i + 1 < len(dedup):
            findings.append(
                {
                    "code": "TOO_SHORT",
                    "message": "%r would last %.1fs (< %ds); merged "
                    "into the previous chapter"
                    % (title, float(end - o), MIN_CHAPTER_S),
                }
            )
            continue
        kept.append((o, title))
    if kept and len(kept) > 1 and tl.total - kept[-1][0] < MIN_CHAPTER_S:
        findings.append(
            {
                "code": "TOO_SHORT",
                "message": "last chapter %r lasts %.1fs (< %ds); "
                "merged into the previous one"
                % (kept[-1][1], float(tl.total - kept[-1][0]), MIN_CHAPTER_S),
            }
        )
        kept.pop()
    valid = len(kept) >= MIN_CHAPTERS
    if not valid:
        findings.append(
            {
                "code": "TOO_FEW",
                "message": "%d chapters; YouTube needs at least %d "
                "(each >= %ds) or it ignores them all"
                % (len(kept), MIN_CHAPTERS, MIN_CHAPTER_S),
            }
        )
    text = "\n".join("%s %s" % (core.fmt_chapter(o), title) for o, title in kept)
    return {
        "chapters": [
            {"time": core.fmt_chapter(o), "seconds": core.fsec(o, 3), "title": title}
            for o, title in kept
        ],
        "description_block": text,
        "youtube_valid": valid,
        "findings": findings,
    }


# -------------------------------------------------------------------- captions

_SRT_TIME = re.compile(
    r"(\d+:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(\d+:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})"
)


def load_cues(path):
    with open(path, encoding="utf-8-sig") as fh:
        text = fh.read().replace("\r\n", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            m = _SRT_TIME.search(line)
            if m:
                a = core.parse_time(m.group(1).replace(",", "."))
                b = core.parse_time(m.group(2).replace(",", "."))
                body = "\n".join(lines[i + 1 :]).strip()
                if body and b > a:
                    cues.append((a, b, body))
                break
    if not cues:
        raise core.ToolError("no caption cues found in %s" % path)
    return cues


def remap_cues(plan, cues, min_len=Fraction(1, 10)):
    tl = Timeline(plan)
    out, dropped = [], 0
    for a, b, body in cues:
        parts = tl.map_range(a, b)
        if not parts:
            dropped += 1
            continue
        lo, hi = (
            parts[0][0],
            parts[-1][1],
        )  # pieces of one cue are adjacent after the cut
        if hi - lo < min_len:
            dropped += 1
            continue
        out.append((lo, hi, body))
    out.sort()
    # No overlaps after remapping: clip each cue at the next cue's start.
    fixed = []
    for i, (a, b, body) in enumerate(out):
        if i + 1 < len(out) and b > out[i + 1][0]:
            b = out[i + 1][0]
        if b - a >= min_len:
            fixed.append((a, b, body))
    return fixed, dropped


def cues_from_words(
    plan, words, max_chars=42, max_lines=2, max_s=Fraction(6), gap_s=Fraction(3, 5)
):
    """Build caption cues from kept words only (removed fillers leave the text)."""
    tl = Timeline(plan)
    kept = []
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        o = tl.map(mid)
        if o is None or not w["word"]:
            continue
        oa = tl.map(w["start"])
        ob = tl.map(w["end"] - Fraction(1, 1000))
        kept.append(
            (oa if oa is not None else o, (ob if ob is not None else o), w["word"])
        )
    cues, cur = [], []

    def flush():
        if cur:
            lines, line = [], ""
            for _, _, word in cur:
                cand = (line + " " + word).strip()
                if len(cand) > max_chars and line:
                    lines.append(line)
                    line = word
                else:
                    line = cand
            lines.append(line)
            cues.append((cur[0][0], cur[-1][1], "\n".join(lines)))
            cur.clear()

    for a, b, word in kept:
        if cur:
            text_len = len(" ".join(x[2] for x in cur)) + 1 + len(word)
            if (
                a - cur[-1][1] > gap_s
                or b - cur[0][0] > max_s
                or text_len > max_chars * max_lines
            ):
                flush()
        cur.append((a, b, word))
    flush()
    return cues


def write_srt(cues, path=None):
    lines = []
    for i, (a, b, body) in enumerate(cues, 1):
        lines += [str(i), "%s --> %s" % (core.fmt_srt(a), core.fmt_srt(b)), body, ""]
    text = "\n".join(lines)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


# -------------------------------------------------------------------- CLI


def _sample():
    plan = {
        "fps": "30",
        "expected_duration": "290",
        "segments": [
            {"src_start_frame": 0, "src_end_frame": 1800, "out_start_frame": 0},
            {"src_start_frame": 2400, "src_end_frame": 9000, "out_start_frame": 1800},
        ],
    }
    chapters = [
        (Fraction(0), "Intro"),
        (Fraction(65), "Setup (cut from the edit)"),
        (Fraction(95), "The build"),
        (Fraction(200), "Results"),
        (Fraction(295), "Outro"),
    ]
    cues = [
        (Fraction(58), Fraction(62), "this sentence crosses the cut"),
        (Fraction(70), Fraction(72), "this one was removed"),
        (Fraction(120), Fraction(123), "and this one moves 20 s earlier"),
    ]
    ch = remap_chapters(plan, chapters)
    cu, dropped = remap_cues(plan, cues)
    tl = Timeline(plan)
    points = {}
    for t in (Fraction(90), Fraction(120)):
        o = tl.map(t)
        points[core.fmt_ts(t)] = core.fmt_ts(o) if o is not None else "CUT"
    return {
        "chapters": ch,
        "captions_srt": write_srt(cu),
        "captions_dropped": dropped,
        "points": points,
    }
