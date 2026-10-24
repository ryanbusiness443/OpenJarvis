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
LOCAL_EFFECTS = {"zoom", "text", "blur"}
GLOBAL_EFFECTS = {"color", "lut", "captions", "music", "fade_in", "fade_out"}
MAX_TRANSITION_S = Fraction(3)

CAPTION_STYLES = {
    # libass force_style; sizes are in the subtitle's 288-line reference frame.
    "youtube": "Fontname=Sans,Fontsize=16,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1.5,Shadow=0,Alignment=2,MarginV=18",
    "shorts": "Fontname=Sans,Fontsize=13,Bold=1,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=0,Alignment=5",
}


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
        if kind not in LOCAL_EFFECTS | GLOBAL_EFFECTS:
            raise core.ToolError("effect %d: unknown type %r" % (i, kind))
        e = {"type": kind, "index": i}
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
        planned.append(e)
    return planned


def build_caption_cues(plan, effect):
    """Caption cues on the output timeline for a burned-in captions effect."""
    from openjarvis.tools.video import remapper as remap

    src = effect["source"]
    if src.lower().endswith((".srt", ".vtt")):
        cues, _ = remap.remap_cues(plan, remap.load_cues(src))
    else:
        cues = remap.cues_from_words(plan, core.load_words(src))
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
    if e["type"] == "text":
        tpath = os.path.join(tmpdir, "text_%d.txt" % k)
        with open(tpath, "w", encoding="utf-8") as fh:
            fh.write(e["text"])
        size_px = max(8, int(round(e["size"] * H)))
        pos = e["position"]
        if pos == "top":
            xy = "x=(w-text_w)/2:y=h*0.08"
        elif pos == "center":
            xy = "x=(w-text_w)/2:y=(h-text_h)/2"
        elif pos == "bottom":
            xy = "x=(w-text_w)/2:y=h*0.92-text_h"
        else:
            xy = "x=%d-text_w/2:y=%d-text_h/2" % (int(pos[0] * W), int(pos[1] * H))
        font = (
            ("fontfile='%s'" % _esc(e["fontfile"]))
            if e.get("fontfile")
            else "font=Sans"
        )
        box = (
            ":box=1:boxcolor=black@0.55:boxborderw=%d" % max(4, size_px // 4)
            if e["box"]
            else ":borderw=2:bordercolor=black"
        )
        return "[%s]drawtext=%s:textfile='%s':fontsize=%d:fontcolor=%s:%s%s%s[%s]" % (
            label_in,
            font,
            _esc(tpath),
            size_px,
            e["color"],
            xy,
            box,
            gate,
            label_out,
        )
    raise core.ToolError("not a local effect: %s" % e["type"])


def captions_filter(plan, e, tmpdir, size):
    from openjarvis.tools.video import remapper as remap

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
