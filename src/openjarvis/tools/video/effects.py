"""Transitions and effects for the video tools.

Shared by the planner (validation, source -> output timing), the renderer
(ffmpeg filters) and the verifier (re-applying an effect to a source frame to
prove the render used it, at the right frames). Transitions use ffmpeg's xfade
styles and shorten the timeline by exactly their overlap; local effects (zoom,
text, blur) are gated by frame number; global effects are color, lut, burned
captions, voice-ducked music, and fades.
"""

from __future__ import annotations

import os
from fractions import Fraction

from openjarvis.tools.video import core

TRANSITIONS = {
    "cut",
    "fade",
    "dissolve",
    "fadeblack",
    "fadewhite",
    "fadegrays",
    "fadefast",
    "fadeslow",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "wipetl",
    "wipetr",
    "wipebl",
    "wipebr",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
    "circleopen",
    "circleclose",
    "circlecrop",
    "rectcrop",
    "vertopen",
    "vertclose",
    "horzopen",
    "horzclose",
    "radial",
    "pixelize",
    "distance",
    "diagtl",
    "diagtr",
    "diagbl",
    "diagbr",
    "hlslice",
    "hrslice",
    "vuslice",
    "vdslice",
    "hblur",
    "squeezeh",
    "squeezev",
    "zoomin",
    "coverleft",
    "coverright",
    "coverup",
    "coverdown",
    "revealleft",
    "revealright",
    "revealup",
    "revealdown",
}
LOCAL_EFFECTS = {"zoom", "text", "blur", "shake", "flash", "glitch", "bw"}
GLOBAL_EFFECTS = {
    "color",
    "lut",
    "captions",
    "music",
    "fade_in",
    "fade_out",
    "progress_bar",
}
POINT_EFFECTS = {"sfx"}  # any number, each pinned to one instant

# Synthesized sound effects: (ffmpeg lavfi source, seconds, default gain dB
# relative to a full-scale hit). Generated with code, so they are royalty-free
# and identical on every machine. Gains are relative to the voice after
# loudness normalisation (see render_engine.py).
SFX_SOUNDS = {
    "whoosh": (
        "anoisesrc=c=pink:r=48000:a=0.9:seed=7:d=0.42,highpass=f=500,lowpass=f=7000,"
        "afade=t=in:d=0.26:curve=exp,afade=t=out:st=0.26:d=0.16",
        0.42,
        -13.0,
    ),
    "boom": (
        "aevalsrc='0.95*sin(2*PI*(48+90*exp(-14*t))*t)*exp(-3.8*t)':s=48000:d=0.9",
        0.9,
        -5.0,
    ),
    "bass_drop": (
        "aevalsrc='0.95*sin(2*PI*(160*exp(-2.2*t)+32)*t)*min(1\\,8*t)*exp(-1.6*t)'"
        ":s=48000:d=1.4",
        1.4,
        -6.0,
    ),
    "pop": (
        "aevalsrc='0.9*sin(2*PI*(1100-5200*t)*t)*exp(-38*t)':s=48000:d=0.12",
        0.12,
        -10.0,
    ),
    "click": ("aevalsrc='0.9*(random(3)*2-1)*exp(-160*t)':s=48000:d=0.05", 0.05, -14.0),
    "ding": (
        "aevalsrc='0.6*sin(2*PI*1760*t)*exp(-5*t)+0.35*sin(2*PI*2640*t)*exp(-7*t)'"
        ":s=48000:d=1.0",
        1.0,
        -12.0,
    ),
    "riser": (
        "aevalsrc='0.5*sin(2*PI*(220*t+380*t*t))*t*0.8+0.25*(random(5)*2-1)*t*t'"
        ":s=48000:d=1.2,highpass=f=120",
        1.2,
        -15.0,
    ),
    "camera": (
        "aevalsrc='0.9*(random(9)*2-1)*(exp(-90*t)+0.6*exp(-90*abs(t-0.09)))'"
        ":s=48000:d=0.18,highpass=f=1500",
        0.18,
        -12.0,
    ),
}
MAX_TRANSITION_S = Fraction(3)

CAPTION_STYLES = {
    # libass force_style; sizes are in the subtitle's 288-line reference frame.
    "youtube": "Fontname=Sans,Fontsize=16,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1.5,Shadow=0,Alignment=2,MarginV=18",
    # Lower third, clear of the face in a centre-cropped vertical frame.
    "shorts": "Fontname=Sans,Fontsize=11,Bold=1,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2.2,Shadow=0,Alignment=2,"
    "MarginV=56",
}
# How burned captions are chunked from word timestamps, per style: long-form
# reads two lines at a time; Shorts flash a few words at a time.
CAPTION_STYLES["viral"] = (
    None  # word-by-word karaoke, rendered from a generated .ass file
)
CAPTION_CHUNKS = {
    "youtube": {"max_chars": 42, "max_lines": 2, "max_s": Fraction(9, 2)},
    "shorts": {"max_chars": 18, "max_lines": 2, "max_s": Fraction(11, 5)},
    "viral": {"max_chars": 14, "max_lines": 2, "max_s": Fraction(8, 5)},
}
TEXT_STYLES = {"box", "hook", "outline"}
VIRAL_HOLD = Fraction(15, 100)  # karaoke: the last word of a phrase lingers this long


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------


def resolve_transition(spec, default, fps):
    """-> (type, frames). spec/default: None | "fade" | {"type","duration"}."""
    spec = spec if spec is not None else default
    if spec is None:
        return "cut", 0
    if isinstance(spec, str):
        spec = {"type": spec}
    kind = spec.get("type", "cut")
    if kind not in TRANSITIONS:
        raise core.ToolError("unknown transition %r; see the effects docs" % kind)
    if kind == "cut":
        return "cut", 0
    dur = core.parse_time(spec.get("duration", "0.5"), fps)
    if dur > MAX_TRANSITION_S:
        raise core.ToolError(
            "transition duration %ss exceeds %ss" % (float(dur), MAX_TRANSITION_S)
        )
    return kind, max(1, core.frame_of(dur, fps))


def apply_transitions(segments, edl, fps, findings):
    """Annotate segments with trans_in/out frames; returns output total frames.

    Piece i starts at P_i = P_{i-1} + n_{i-1} - d_i, where d_i is the overlap
    of the transition into piece i.
    """
    default = edl.get("transition")
    prev = None
    pos = 0
    for i, s in enumerate(segments):
        n = s["src_end_frame"] - s["src_start_frame"]
        kind, d = (
            ("cut", 0)
            if i == 0
            else resolve_transition(s.pop("trans_req", None), default, fps)
        )
        s.pop("trans_req", None)
        if d and prev is not None:
            limit = min(prev["frames"], n) - 1
            if d > limit:
                findings.append(
                    {
                        "severity": "warn",
                        "code": "TRANSITION_SHORTENED",
                        "message": "%s into piece %d wanted %d frames; pieces allow %d"
                        % (kind, i, d, max(limit, 0)),
                        "fix": "lengthen the neighbouring pieces or shorten it",
                    }
                )
                d = max(limit, 0)
                kind = kind if d else "cut"
        s["trans_in_type"] = kind
        s["trans_in_frames"] = d
        s["trans_out_frames"] = 0
        if prev is not None:
            prev["trans_out_frames"] = d
            pos = pos - d
        s["frames"] = n
        s["out_start_frame"] = pos
        s["out_end_frame"] = pos + n
        pos += n
        prev = s
    for s in segments:
        clean = s["frames"] - s["trans_in_frames"] - s["trans_out_frames"]
        if clean < 1:
            findings.append(
                {
                    "severity": "block",
                    "code": "TRANSITION_TOO_LONG",
                    "message": "piece %d (%d frames) is consumed by its transitions "
                    "(%d in, %d out)"
                    % (
                        s["index"] if "index" in s else -1,
                        s["frames"],
                        s["trans_in_frames"],
                        s["trans_out_frames"],
                    ),
                    "fix": "shorten the transitions or keep more of this piece",
                }
            )
    return pos


def has_transitions(plan):
    return any(s.get("trans_in_frames") for s in plan["segments"])


# --------------------------------------------------------------------------
# Effects: validation + source -> output timing
# --------------------------------------------------------------------------


def _map_range(segments, fps, a, b):
    """Source range -> (out_start_frame, out_end_frame) or None."""
    fa, fb = core.frame_of(a, fps), core.frame_of(b, fps)
    lo = hi = None
    for s in segments:
        x, y = max(fa, s["src_start_frame"]), min(fb, s["src_end_frame"])
        if y > x:
            oa = s["out_start_frame"] + (x - s["src_start_frame"])
            ob = s["out_start_frame"] + (y - s["src_start_frame"])
            lo = oa if lo is None else min(lo, oa)
            hi = ob if hi is None else max(hi, ob)
    return None if lo is None else (lo, hi)


def _resolve(path, base):
    if not path:
        raise core.ToolError("effect needs a file path")
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(base, path))
    if not os.path.isfile(path):
        raise core.ToolError("effect file not found: %s" % path)
    return path


def _frac01(v, name):
    v = float(v)
    if not 0.0 <= v <= 1.0:
        raise core.ToolError("%s must be a fraction 0..1, got %s" % (name, v))
    return v


def plan_effects(edl, segments, total_frames, fps, base_dir, findings):
    """Validate EDL effects and resolve them onto the output frame timeline."""
    planned = []
    seen_global = set()
    for i, eff in enumerate(edl.get("effects") or []):
        kind = eff.get("type")
        if kind not in LOCAL_EFFECTS | GLOBAL_EFFECTS | POINT_EFFECTS:
            raise core.ToolError("effect %d: unknown type %r" % (i, kind))
        e = {"type": kind, "index": i}
        if kind == "sfx":
            sound = eff.get("sound")
            if sound not in SFX_SOUNDS:
                raise core.ToolError(
                    "sfx %d: sound must be one of %s"
                    % (i, ", ".join(sorted(SFX_SOUNDS)))
                )
            at = core.parse_time(eff.get("at", eff.get("start", 0)), fps)
            if eff.get("time") == "output":
                f = core.frame_of(at, fps)
                frame = f if f < total_frames else None
            else:
                rng = _map_range(segments, fps, at, at + Fraction(1) / fps)
                frame = rng[0] if rng else None
            if frame is None and eff.get("time") != "output":
                # Inside a removed pause: play it on the first kept frame after
                # the cut, which is where a hit on that cut belongs anyway.
                f_at = core.frame_of(at, fps)
                later = [sg for sg in segments if sg["src_start_frame"] >= f_at]
                if later:
                    frame = min(later, key=lambda sg: sg["src_start_frame"])[
                        "out_start_frame"
                    ]
                    findings.append(
                        {
                            "severity": "info",
                            "code": "SFX_MOVED",
                            "message": "sfx %d (%s at %s) was inside a cut; moved to "
                            "the next kept frame" % (i, sound, core.fmt_ts(at)),
                        }
                    )
            if frame is None:
                findings.append(
                    {
                        "severity": "warn",
                        "code": "EFFECT_IN_CUT",
                        "message": "sfx %d (%s at %s) lands in removed material; "
                        "dropped" % (i, sound, core.fmt_ts(at)),
                    }
                )
                continue
            e.update(
                {
                    "sound": sound,
                    "out_frame": frame,
                    "gain_db": float(eff.get("gain_db", SFX_SOUNDS[sound][2])),
                }
            )
            planned.append(e)
            continue
        if kind in LOCAL_EFFECTS:
            a = core.parse_time(eff["start"], fps)
            b = core.parse_time(eff["end"], fps)
            if b <= a:
                raise core.ToolError("effect %d (%s): end <= start" % (i, kind))
            if eff.get("time") == "output":
                rng = (core.frame_of(a, fps), min(core.frame_of(b, fps), total_frames))
                rng = rng if rng[1] > rng[0] else None
            else:
                rng = _map_range(segments, fps, a, b)
            if not rng:
                findings.append(
                    {
                        "severity": "warn",
                        "code": "EFFECT_IN_CUT",
                        "message": "%s effect %d (%s-%s) lies entirely in removed "
                        "material; dropped" % (kind, i, core.fmt_ts(a), core.fmt_ts(b)),
                    }
                )
                continue
            e["out_start_frame"], e["out_end_frame"] = rng
            for s in segments:
                lo = s["out_start_frame"]
                if (
                    s["trans_in_frames"]
                    and lo < rng[1]
                    and rng[0] < lo + s["trans_in_frames"]
                ):
                    findings.append(
                        {
                            "severity": "warn",
                            "code": "EFFECT_OVER_TRANSITION",
                            "message": "%s effect %d overlaps the transition into "
                            "piece %d" % (kind, i, s["index"]),
                            "fix": "move it clear of the transition so it can be "
                            "verified",
                        }
                    )
            if kind == "zoom":
                e["scale"] = float(eff.get("scale", 1.2))
                if not 1.01 <= e["scale"] <= 4.0:
                    raise core.ToolError("zoom scale must be 1.01..4.0")
                cx, cy = eff.get("center", [0.5, 0.5])
                e["center"] = [_frac01(cx, "center x"), _frac01(cy, "center y")]
            elif kind == "text":
                text = str(eff.get("text", "")).strip()
                if not text or len(text) > 200:
                    raise core.ToolError("text effect %d needs 1-200 characters" % i)
                e["text"] = text
                pos = eff.get("position", "bottom")
                if isinstance(pos, list):
                    pos = [_frac01(pos[0], "text x"), _frac01(pos[1], "text y")]
                elif pos not in ("top", "center", "bottom"):
                    raise core.ToolError(
                        "text position must be top|center|bottom|[x,y]"
                    )
                e["position"] = pos
                e["size"] = float(eff.get("size", 0.06))
                if not 0.01 <= e["size"] <= 0.3:
                    raise core.ToolError(
                        "text size is a fraction of frame height, 0.01..0.3"
                    )
                e["color"] = str(eff.get("color", "white"))
                e["box"] = bool(eff.get("box", True))
                e["style"] = eff.get("style", "box" if e["box"] else "outline")
                if e["style"] not in TEXT_STYLES:
                    raise core.ToolError(
                        "text style must be one of %s" % ", ".join(sorted(TEXT_STYLES))
                    )
                if eff.get("fontfile"):
                    e["fontfile"] = _resolve(eff["fontfile"], base_dir)
            elif kind == "blur":
                rect = eff.get("rect")
                if not rect or len(rect) != 4:
                    raise core.ToolError("blur effect %d needs rect [x,y,w,h]" % i)
                x, y, w, h = (_frac01(v, "blur rect") for v in rect)
                if w <= 0 or h <= 0 or x + w > 1.0001 or y + h > 1.0001:
                    raise core.ToolError("blur rect must lie inside the frame")
                e["rect"] = [x, y, w, h]
                e["strength"] = int(eff.get("strength", 20))
            elif kind == "shake":
                e["intensity"] = float(eff.get("intensity", 0.02))
                if not 0.003 <= e["intensity"] <= 0.06:
                    raise core.ToolError(
                        "shake intensity is a fraction of width, 0.003..0.06"
                    )
                e["speed"] = float(eff.get("speed", 11.0))
            elif kind == "flash":
                e["fps"] = core.frac_str(fps)
                e["peak"] = float(eff.get("peak", 0.85))
                if not 0.05 <= e["peak"] <= 1.0:
                    raise core.ToolError("flash peak must be 0.05..1")
                e["color"] = eff.get("color", "white")
                if e["color"] not in ("white", "black"):
                    raise core.ToolError("flash color must be white or black")
            elif kind == "glitch":
                e["amount"] = float(eff.get("amount", 0.012))
                if not 0.002 <= e["amount"] <= 0.05:
                    raise core.ToolError(
                        "glitch amount is a fraction of width, 0.002..0.05"
                    )
        else:
            if kind in seen_global:
                raise core.ToolError("only one %s effect is allowed" % kind)
            seen_global.add(kind)
            if kind == "color":
                for k, lo, hi, dv in (
                    ("brightness", -1, 1, 0.0),
                    ("contrast", 0, 3, 1.0),
                    ("saturation", 0, 3, 1.0),
                    ("gamma", 0.1, 10, 1.0),
                ):
                    v = float(eff.get(k, dv))
                    if not lo <= v <= hi:
                        raise core.ToolError("color %s must be %s..%s" % (k, lo, hi))
                    e[k] = v
            elif kind == "lut":
                e["file"] = _resolve(eff.get("file"), base_dir)
            elif kind == "captions":
                e["source"] = _resolve(eff.get("source"), base_dir)
                e["style"] = eff.get("style", "youtube")
                if e["style"] not in CAPTION_STYLES:
                    raise core.ToolError(
                        "captions style must be one of %s"
                        % ", ".join(sorted(CAPTION_STYLES))
                    )
            elif kind == "music":
                e["file"] = _resolve(eff.get("file"), base_dir)
                e["volume_db"] = float(eff.get("volume_db", -18))
                e["duck"] = bool(eff.get("duck", True))
                e["fade_in"] = float(eff.get("fade_in", 1.0))
                e["fade_out"] = float(eff.get("fade_out", 2.0))
            elif kind in ("fade_in", "fade_out"):
                d = core.parse_time(eff.get("duration", "0.5"), fps)
                e["frames"] = max(1, min(core.frame_of(d, fps), total_frames))
            elif kind == "progress_bar":
                e["color"] = str(eff.get("color", "0xFFD400"))
                e["height"] = float(eff.get("height", 0.008))
                if not 0.002 <= e["height"] <= 0.03:
                    raise core.ToolError(
                        "progress_bar height is a fraction of frame height"
                    )
                e["position"] = eff.get("position", "top")
                if e["position"] not in ("top", "bottom"):
                    raise core.ToolError("progress_bar position must be top or bottom")
                e["total_s"] = float(core.frame_time(total_frames, fps))
        planned.append(e)
    return planned


def build_caption_cues(plan, effect):
    """Caption cues on the output timeline for a burned-in captions effect."""
    from openjarvis.tools.video import remapper as remap

    src = effect["source"]
    if src.lower().endswith((".srt", ".vtt")):
        cues, _ = remap.remap_cues(plan, remap.load_cues(src))
    elif effect.get("style") == "viral":
        # Displayed spans, exactly as write_viral_ass shows them: the last word
        # is held VIRAL_HOLD longer, cut short by the next phrase.
        groups = remap.word_groups(
            plan, core.load_words(src), **CAPTION_CHUNKS["viral"]
        )
        cues = []
        for gi, g in enumerate(groups):
            end = g[-1][1] + VIRAL_HOLD
            if gi + 1 < len(groups):
                end = min(end, groups[gi + 1][0][0])
            cues.append((g[0][0], end, " ".join(w.upper() for _, _, w in g)))
    else:
        cues = remap.cues_from_words(
            plan,
            core.load_words(src),
            **CAPTION_CHUNKS.get(effect.get("style", "youtube"), {}),
        )
    return cues


# --------------------------------------------------------------------------
# Geometry + filters (shared by renderer and verifier)
# --------------------------------------------------------------------------


def output_size(info, preset_cfg):
    """Exact output frame size for a probe() result and a preset."""
    v = info.get("video") or {}
    w, h = v.get("width") or 0, v.get("height") or 0
    if abs(v.get("rotation") or 0) in (90, 270):
        w, h = h, w  # ffmpeg autorotates on decode
    size = preset_cfg.get("size")
    if size and preset_cfg.get("fill") == "crop":
        return size
    if size:
        r = min(Fraction(size[0], w), Fraction(size[1], h))
        return (int(w * r) // 2 * 2, int(h * r) // 2 * 2)
    return (w // 2 * 2, h // 2 * 2)


def geometry_filters(info, preset_cfg):
    """Crop/scale filters that take a decoded source frame to the output size."""
    W, H = output_size(info, preset_cfg)
    parts = []
    if preset_cfg.get("size") and preset_cfg.get("fill") == "crop":
        bw, bh = preset_cfg["size"]
        parts.append(
            "crop='min(iw\\,ih*%d/%d)':'min(ih\\,iw*%d/%d)'" % (bw, bh, bh, bw)
        )
    parts.append("scale=%d:%d:flags=lanczos" % (W, H))
    parts.append("setsar=1")
    return parts, (W, H)


def _esc(path):
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def color_filters(effects):
    out = []
    for e in effects:
        if e["type"] == "color":
            out.append(
                "eq=brightness=%s:contrast=%s:saturation=%s:gamma=%s"
                % (e["brightness"], e["contrast"], e["saturation"], e["gamma"])
            )
        elif e["type"] == "lut":
            out.append("lut3d=file='%s'" % _esc(e["file"]))
    return out


_HEAVY = {}


def heavy_font():
    """(font file, family) of the heaviest installed sans face, via fontconfig.

    drawtext ignores a style in its `font` option, so a bold look needs the
    file itself. Falls back to (None, "Sans") where fontconfig is missing.
    """
    if "v" not in _HEAVY:
        import shutil
        import subprocess

        found = (None, "Sans")
        if shutil.which("fc-match"):
            for pattern in ("Sans:style=Black", "Sans:style=ExtraBold", "Sans:bold"):
                out = subprocess.run(
                    ["fc-match", "-f", "%{file}\t%{family[0]}", pattern],
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if "\t" in out and os.path.isfile(out.split("\t")[0]):
                    found = tuple(out.split("\t", 1))
                    break
        _HEAVY["v"] = found
    return _HEAVY["v"]


def fps_of_size(e):
    return core.to_fraction(e["fps"])


def progress_bar_filter(e, size):
    W, H = size
    h = max(2, int(round(e["height"] * H)))
    y = 0 if e["position"] == "top" else H - h
    return "drawbox=x=0:y=%d:w='iw*min(1\\,t/%.6f)':h=%d:color=%s@1:t=fill" % (
        y,
        e["total_s"],
        h,
        e["color"],
    )


def local_effect_graph(e, size, label_in, label_out, tmpdir, enable=True):
    """Filtergraph fragment applying one local effect: [label_in] -> [label_out].

    With enable=False the effect applies to every frame (verifier use on a
    single source frame).
    """
    W, H = size
    gate = (
        ":enable='between(n\\,%d\\,%d)'"
        % (e["out_start_frame"], e["out_end_frame"] - 1)
        if enable
        else ""
    )
    k = e["index"]
    if e["type"] == "zoom":
        s = e["scale"]
        cw = max(2, int(W / s) // 2 * 2)
        ch = max(2, int(H / s) // 2 * 2)
        x = int(round(min(max(e["center"][0] * W - cw / 2, 0), W - cw)))
        y = int(round(min(max(e["center"][1] * H - ch / 2, 0), H - ch)))
        return (
            "[%s]split=2[zb%d][zi%d];[zi%d]crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos,"
            "setsar=1[zo%d];[zb%d][zo%d]overlay=0:0%s[%s]"
            % (label_in, k, k, k, cw, ch, x, y, W, H, k, k, k, gate, label_out)
        )
    if e["type"] == "blur":
        x, y, w, h = e["rect"]
        rx, ry = int(x * W) // 2 * 2, int(y * H) // 2 * 2
        rw = max(4, min(int(w * W) // 2 * 2, W - rx))
        rh = max(4, min(int(h * H) // 2 * 2, H - ry))
        lr = max(1, min(e["strength"], rw // 2 - 1, rh // 2 - 1))
        cr = max(1, min(lr, rw // 4 - 1, rh // 4 - 1))
        return (
            "[%s]split=2[bb%d][bi%d];[bi%d]crop=%d:%d:%d:%d,boxblur=luma_radius=%d:"
            "luma_power=2:chroma_radius=%d:chroma_power=2[bo%d];[bb%d][bo%d]overlay=%d:%d%s[%s]"
            % (
                label_in,
                k,
                k,
                k,
                rw,
                rh,
                rx,
                ry,
                lr,
                cr,
                k,
                k,
                k,
                rx,
                ry,
                gate,
                label_out,
            )
        )
    if e["type"] == "shake":
        # Time-based (t), not frame-based, so the verifier can reproduce any
        # single frame by stamping it with its output time first.
        m = max(2, int(e["intensity"] * W) // 2 * 2)
        f1, f2 = e["speed"], e["speed"] * 1.37
        return (
            "[%s]split=2[sb%d][si%d];[si%d]crop=%d:%d:x='%d+%d*sin(2*PI*%.3f*t)':"
            "y='%d+%d*cos(2*PI*%.3f*t)',scale=%d:%d:flags=bicubic,setsar=1[so%d];"
            "[sb%d][so%d]overlay=0:0%s[%s]"
            % (
                label_in,
                k,
                k,
                k,
                W - 2 * m,
                H - 2 * m,
                m,
                m,
                f1,
                m,
                m,
                f2,
                W,
                H,
                k,
                k,
                k,
                gate,
                label_out,
            )
        )
    if e["type"] == "flash":
        t0 = float(e["out_start_frame"]) / float(fps_of_size(e))
        t1 = float(e["out_end_frame"]) / float(fps_of_size(e))
        level = "white" if e["color"] == "white" else "black"
        # Peak at the first frame, decaying linearly to nothing at the last.
        k_expr = "%.4f*clip((%.6f-T)/%.6f\\,0\\,1)" % (
            e["peak"],
            t1,
            max(t1 - t0, 1e-6),
        )
        return (
            "[%s]split=2[fb%d][fi%d];[fi%d]drawbox=0:0:iw:ih:color=%s@1:t=fill[fw%d];"
            "[fb%d][fw%d]blend=all_expr='A*(1-%s)+B*%s'%s[%s]"
            % (label_in, k, k, k, level, k, k, k, k_expr, k_expr, gate, label_out)
        )
    if e["type"] == "glitch":
        a = max(2, int(e["amount"] * W))
        return "[%s]rgbashift=rh=-%d:bh=%d:gv=%d:edge=smear%s,format=yuv420p[%s]" % (
            label_in,
            a,
            a,
            max(1, a // 3),
            gate,
            label_out,
        )
    if e["type"] == "bw":
        return "[%s]hue=s=0%s[%s]" % (label_in, gate, label_out)
    if e["type"] == "text":
        tpath = os.path.join(tmpdir, "text_%d.txt" % k)
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write(e["text"])
        size_px = max(8, int(round(e["size"] * H)))
        # Never let a line run off the frame: estimate its width from the
        # longest line (heavy caps run ~0.68 em per character, regular ~0.55)
        # and shrink the font until it fits in 90% of the width.
        longest = max(len(line) for line in e["text"].split("\n"))
        em = 0.68 if e.get("style", "box") != "box" else 0.55
        if longest * em * size_px > 0.9 * W:
            size_px = max(8, int(0.9 * W / (longest * em)))
        pos = e["position"]
        if pos == "top":
            xy = "x=(w-text_w)/2:y=h*0.08"
        elif pos == "center":
            xy = "x=(w-text_w)/2:y=(h-text_h)/2"
        elif pos == "bottom":
            xy = "x=(w-text_w)/2:y=h*0.92-text_h"
        else:
            xy = "x=%d-text_w/2:y=%d-text_h/2" % (int(pos[0] * W), int(pos[1] * H))
        style = e.get("style", "box")
        heavy = heavy_font()[0]
        if e.get("fontfile"):
            font = "fontfile='%s'" % _esc(e["fontfile"])
        elif style != "box" and heavy:
            font = "fontfile='%s'" % _esc(heavy)
        else:
            font = "font='Sans'"
        color = e["color"]
        if style == "hook":
            # Black on a solid yellow block: the short-form "title card" look.
            color = "black" if color == "white" else color
            box = ":box=1:boxcolor=0xFFD400@1:boxborderw=%d" % max(6, size_px // 3)
        elif style == "outline":
            box = ":borderw=%d:bordercolor=black" % max(2, size_px // 12)
        else:
            box = ":box=1:boxcolor=black@0.55:boxborderw=%d" % max(4, size_px // 4)
        return "[%s]drawtext=%s:textfile='%s':fontsize=%d:fontcolor=%s:%s%s%s[%s]" % (
            label_in,
            font,
            _esc(tpath),
            size_px,
            color,
            xy,
            box,
            gate,
            label_out,
        )
    raise core.ToolError("not a local effect: %s" % e["type"])


def _ass_time(t):
    t = Fraction(t)
    cs = int(t * 100 + Fraction(1, 2))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    sec, c = divmod(rem, 100)
    return "%d:%02d:%02d.%02d" % (h, m, sec, c)


def write_viral_ass(plan, e, path, size):
    """Word-by-word karaoke captions: the spoken word turns yellow and pops.

    One event per word, so each word's highlight starts on its own timestamp
    (to the centisecond ASS allows) and the text never runs ahead of speech.
    """
    from openjarvis.tools.video import remapper as remap

    W, H = size
    groups = remap.word_groups(
        plan, core.load_words(e["source"]), **CAPTION_CHUNKS["viral"]
    )
    fs = int(round(H * (0.052 if H > W else 0.075)))
    margin_v = int(round(H * (0.24 if H > W else 0.10)))
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Viral,%s,%d,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,"
        "100,100,0,0,1,%d,%d,2,40,40,%d,1\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, "
        "Text\n"
        % (W, H, heavy_font()[1], fs, max(3, fs // 9), max(1, fs // 22), margin_v)
    )
    lines = []
    cues = []
    max_chars = CAPTION_CHUNKS["viral"]["max_chars"]
    for gi, g in enumerate(groups):
        words = [w.upper() for _, _, w in g]
        cues.append((g[0][0], g[-1][1], " ".join(words)))
        # Line breaks are fixed per group so the text never reflows word to word.
        breaks, width = set(), 0
        for j, w in enumerate(words):
            if j and width + 1 + len(w) > max_chars:
                breaks.add(j)
                width = len(w)
            else:
                width += (1 if j else 0) + len(w)
        next_start = groups[gi + 1][0][0] if gi + 1 < len(groups) else None
        for i, (a, b, _) in enumerate(g):
            start = g[0][0] if i == 0 else a
            end = g[i + 1][0] if i + 1 < len(g) else b + VIRAL_HOLD
            if next_start is not None:
                end = min(end, next_start)  # never two phrases on screen at once
            if end <= start:
                continue
            text = ""
            for j, w in enumerate(words):
                if j:
                    text += "\\N" if j in breaks else " "
                if j == i:
                    text += (
                        "{\\c&H00D4FF&\\fscx118\\fscy118\\t(0,90,\\fscx100"
                        "\\fscy100)}%s{\\r}" % w
                    )
                else:
                    text += w
            lines.append(
                "Dialogue: 0,%s,%s,Viral,,0,0,0,,%s"
                % (_ass_time(start), _ass_time(end), text)
            )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head + "\n".join(lines) + "\n")
    return cues


def captions_filter(plan, e, tmpdir, size):
    from openjarvis.tools.video import remapper as remap

    if e["style"] == "viral":
        ass = os.path.join(tmpdir, "burn_%d.ass" % e["index"])
        cues = write_viral_ass(plan, e, ass, size)
        return "subtitles=filename='%s'" % _esc(ass), cues
    srt = os.path.join(tmpdir, "burn_%d.srt" % e["index"])
    cues = build_caption_cues(plan, e)
    remap.write_srt(cues, srt)
    return (
        "subtitles=filename='%s':original_size=%dx%d:force_style='%s'"
        % (_esc(srt), size[0], size[1], CAPTION_STYLES[e["style"]])
    ), cues


def effects_video_graph(plan, effects, size, label_in, tmpdir):
    """Chain all video effects after assembly. Returns (graph_text, label_out)."""
    lines, cur = [], label_in
    globals_ = color_filters(effects)
    if globals_:
        lines.append("[%s]%s[fxc]" % (cur, ",".join(globals_)))
        cur = "fxc"
    for e in effects:
        if e["type"] in LOCAL_EFFECTS:
            nxt = "fx%d" % e["index"]
            lines.append(local_effect_graph(e, size, cur, nxt, tmpdir))
            cur = nxt
    for e in effects:
        if e["type"] == "captions":
            filt, _ = captions_filter(plan, e, tmpdir, size)
            lines.append("[%s]%s[fxs]" % (cur, filt))
            cur = "fxs"
    for e in effects:
        if e["type"] == "progress_bar":
            lines.append("[%s]%s[fxp]" % (cur, progress_bar_filter(e, size)))
            cur = "fxp"
    total = plan["total_frames"]
    fades = []
    for e in effects:
        if e["type"] == "fade_in":
            fades.append("fade=t=in:s=0:n=%d" % e["frames"])
        elif e["type"] == "fade_out":
            fades.append("fade=t=out:s=%d:n=%d" % (total - e["frames"], e["frames"]))
    if fades:
        lines.append("[%s]%s[fxf]" % (cur, ",".join(fades)))
        cur = "fxf"
    return ";\n".join(lines), cur


def audio_fade_filters(plan, effects):
    fps = core.to_fraction(plan["fps"])
    total = core.frame_time(plan["total_frames"], fps)
    out = []
    for e in effects:
        if e["type"] == "fade_in":
            out.append("afade=t=in:d=%.6f" % float(core.frame_time(e["frames"], fps)))
        elif e["type"] == "fade_out":
            d = core.frame_time(e["frames"], fps)
            out.append("afade=t=out:st=%.6f:d=%.6f" % (float(total - d), float(d)))
    return out


def music_effect(effects):
    return next((e for e in effects if e["type"] == "music"), None)


def sfx_effects(effects):
    return [e for e in effects if e["type"] == "sfx"]


def synth_sfx(sound, path):
    """Render one synthesized sound effect to a 48 kHz mono WAV (deterministic)."""
    ffmpeg, _ = core.require_ffmpeg()
    src, dur, _ = SFX_SOUNDS[sound]
    graph = src
    if not src.startswith(("aevalsrc", "anoisesrc")):
        raise core.ToolError("bad sfx recipe for %s" % sound)
    core.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            graph,
            "-t",
            "%.3f" % dur,
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "pcm_s16le",
            path,
        ]
    )
    return path
