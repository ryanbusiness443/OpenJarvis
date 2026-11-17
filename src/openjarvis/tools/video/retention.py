"""Score an edit plan for short-form retention (0-100) with fixes.

Hook speed, hook visual and text, longest still stretch, change rate,
captions, length, sound-effect density, call to action and loop ending, each
labelled with the evidence level of the rule behind it.
"""

from __future__ import annotations

import re
from fractions import Fraction

from openjarvis.tools.video import core

CTA_WORDS = {
    "follow",
    "subscribe",
    "link",
    "tap",
    "click",
    "comment",
    "share",
    "save",
    "buy",
    "sign",
    "dm",
    "download",
    "join",
    "book",
    "grab",
    "visit",
}
TARGETS = {
    # format: (max still s, min changes/10s, ideal length range s, hard max s)
    "short": (3.0, 3.0, (15, 60), 180),
    "long": (6.0, 1.2, (240, 1200), None),
}


def _row(rows, code, ok, weight, detail, fix, evidence, partial=None):
    score = weight if ok else (partial if partial is not None else 0)
    rows.append(
        {
            "check": code,
            "status": "PASS" if ok else ("PARTIAL" if partial else "FAIL"),
            "points": round(score, 1),
            "of": weight,
            "detail": detail,
            "fix": None if ok else fix,
            "evidence": evidence,
        }
    )


def audit(plan, fmt="short"):
    from openjarvis.tools.video import remapper as remap

    fps = core.to_fraction(plan["fps"])
    total = core.frame_time(plan["total_frames"], fps)
    effects = plan.get("effects") or []
    max_still, min_rate, (lo, hi), hard_max = TARGETS[fmt]
    rows = []

    words = core.load_words(plan["words"]) if plan.get("words") else []
    tl = remap.Timeline(plan)
    kept = [
        (tl.map(w["start"]), tl.map(w["end"] - Fraction(1, 1000)), w["word"])
        for w in words
    ]
    kept = [k for k in kept if k[0] is not None]

    # Visual change events on the output timeline.
    events = {Fraction(0), total}
    for s in plan["segments"][1:]:
        events.add(core.frame_time(s["out_start_frame"], fps))
    for e in effects:
        if "out_start_frame" in e:
            events.add(core.frame_time(e["out_start_frame"], fps))
            events.add(core.frame_time(e["out_end_frame"], fps))
    ev = sorted(t for t in events if t <= total)
    gaps = [b - a for a, b in zip(ev, ev[1:])]
    longest = max(gaps) if gaps else total
    changes = max(0, len(ev) - 2)
    rate = changes / float(total) * 10 if total else 0

    first_word = kept[0][0] if kept else None
    _row(
        rows,
        "HOOK_SPEED",
        first_word is not None and first_word <= Fraction(3, 10),
        15,
        "first word at %s"
        % ("%.2fs" % float(first_word) if first_word is not None else "none"),
        "cut the lead-in so the first word lands on frame 1",
        "editor practice",
        partial=7 if first_word is not None and first_word <= 1 else None,
    )
    early = [
        e for e in effects if "out_start_frame" in e and e["out_start_frame"] <= fps
    ]
    _row(
        rows,
        "HOOK_VISUAL",
        bool(early),
        10,
        "%d effect(s) start in the first second" % len(early),
        "add a title card, punch-in or flash on frame 1",
        "editor practice",
    )
    hook_text = [
        e for e in effects if e["type"] == "text" and e["out_start_frame"] <= 3 * fps
    ]
    has_caps = any(e["type"] == "captions" for e in effects)
    _row(
        rows,
        "HOOK_TEXT",
        bool(hook_text) or has_caps,
        10,
        "hook title %s, captions %s"
        % ("yes" if hook_text else "no", "yes" if has_caps else "no"),
        "put the promise on screen in the first 3 s: many viewers start muted",
        "third-party survey",
    )
    _row(
        rows,
        "STILLNESS",
        longest <= max_still,
        15,
        "longest still stretch %.1fs (target <= %.1fs)" % (float(longest), max_still),
        "add a cut, punch-in or framing change inside the %.1fs stretch at %s"
        % (float(longest), core.fmt_ts(ev[gaps.index(longest)] if gaps else 0)),
        "editor practice",
        partial=7 if longest <= 2 * max_still else None,
    )
    _row(
        rows,
        "CHANGE_RATE",
        rate >= min_rate,
        10,
        "%.1f visual changes per 10s (target >= %.1f)" % (rate, min_rate),
        "alternate framing per sentence and punch on key words",
        "editor practice",
        partial=5 if rate >= min_rate / 2 else None,
    )
    _row(
        rows,
        "CAPTIONS",
        has_caps or fmt == "long",
        10,
        "burned captions %s" % ("present" if has_caps else "missing"),
        "burn captions in (style viral for reels)",
        "third-party survey",
    )
    length_ok = lo <= float(total) <= hi
    over = hard_max is not None and float(total) > hard_max
    _row(
        rows,
        "LENGTH",
        length_ok and not over,
        10,
        "%.1fs (useful range %d-%ds%s)"
        % (float(total), lo, hi, ", hard max %ds" % hard_max if hard_max else ""),
        "trim to the strongest %d-%ds" % (lo, hi)
        if float(total) > hi
        else "this is short; make sure it still lands one complete idea",
        "platform-documented limit; range is practice",
        partial=5 if not over else None,
    )
    sfx = [e for e in effects if e["type"] == "sfx"]
    per_s = len(sfx) / float(total) if total else 0
    _row(
        rows,
        "SFX_DENSITY",
        0 < per_s <= 1.0 or fmt == "long",
        5,
        "%d sound effects (%.2f per second)" % (len(sfx), per_s),
        "add a hit on the hook and whooshes on cuts"
        if not sfx
        else "thin them out: more than one a second becomes noise",
        "editor practice",
    )
    cta = any(re.sub(r"[^a-z]", "", w.lower()) in CTA_WORDS for _, _, w in kept)
    _row(
        rows,
        "CTA",
        cta,
        5,
        "call to action %s" % ("spoken" if cta else "not found"),
        "end with one clear ask (follow, comment, link)",
        "editor practice",
    )
    if fmt == "short":
        fade = any(e["type"] == "fade_out" for e in effects)
        tail = total - kept[-1][1] if kept else total
        _row(
            rows,
            "LOOP",
            not fade and tail <= Fraction(3, 10),
            10,
            "fade-out %s, %.2fs after the last word"
            % ("yes" if fade else "no", float(tail)),
            "end hard on the last word with no fade so the replay loops",
            "editor practice",
            partial=5 if not fade else None,
        )
    else:
        rows.append(
            {
                "check": "LOOP",
                "status": "N/A",
                "points": 10,
                "of": 10,
                "detail": "long-form",
                "fix": None,
                "evidence": "-",
            }
        )

    score = sum(r["points"] for r in rows)
    grade = "READY TO POST" if score >= 90 else ("GOOD" if score >= 75 else "REWORK")
    return {
        "score": round(score, 1),
        "grade": grade,
        "format": fmt,
        "length_s": round(float(total), 2),
        "changes_per_10s": round(rate, 2),
        "longest_still_s": round(float(longest), 2),
        "checks": rows,
        "fixes": [r["fix"] for r in rows if r["fix"]],
        "caveat": "A retention score predicts pacing, not virality: the idea, the "
        "first sentence and the topic decide far more than any edit.",
    }
