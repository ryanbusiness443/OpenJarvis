"""Prove a render matches its plan.

Checks: exact frame count, exact frame rate, duration, A/V sync, per-piece
audio cross-correlation against the source (within 1 ms), frame matching at
every cut (best source match must be offset 0 of -2..+2), loudness and
upload spec. Static shots and periodic audio report AMBIGUOUS, not PASS.
"""

from __future__ import annotations

import os
import struct
import tempfile
from fractions import Fraction

from openjarvis.tools.video import core
from openjarvis.tools.video import effects as fx

MATCH_MAX_MAD = 12.0  # best-match thumbnail difference above this = wrong content
TEXT_THUMB = (192, 108)  # text and captions are checked at 3x thumbnail size
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
    return core.measure_loudness(path)


class _Ctx:
    """Everything the frame checks share: geometry, masks, and busy frames."""

    def __init__(self, plan, tmpdir):
        from openjarvis.tools.video.planner import PRESETS

        self.plan = plan
        self.fps = core.to_fraction(plan["fps"])
        self.tmp = tmpdir
        self.effects = plan.get("effects") or []
        self.info = core.probe(plan["source"])
        cfg = PRESETS.get(plan["output"].get("preset", "youtube"), {})
        self.geo, self.size = fx.geometry_filters(self.info, cfg)
        self.color = fx.color_filters(self.effects)
        self.base_vf = ",".join(self.geo + self.color)
        cap = next((e for e in self.effects if e["type"] == "captions"), None)
        # Burned captions are re-rendered onto source frames at the matching
        # output time, so comparisons stay full-frame and exact.
        self.cap_filter = (
            fx.captions_filter(plan, cap, tmpdir, self.size)[0] if cap else None
        )
        self.busy = {}  # out frame -> why it cannot match a plain source frame
        total = plan["total_frames"]
        for e in self.effects:
            if "out_start_frame" in e:
                for f in range(e["out_start_frame"], e["out_end_frame"]):
                    self.busy.setdefault(f, "%s effect" % e["type"])
            elif e["type"] == "fade_in":
                for f in range(0, e["frames"]):
                    self.busy.setdefault(f, "fade in")
            elif e["type"] == "fade_out":
                for f in range(total - e["frames"], total):
                    self.busy.setdefault(f, "fade out")
        for sgm in plan["segments"]:
            for f in range(
                sgm["out_start_frame"],
                sgm["out_start_frame"] + sgm["trans_in_frames"]
                if sgm.get("trans_in_frames")
                else sgm["out_start_frame"],
            ):
                self.busy.setdefault(f, "transition")

    def src_frame(self, o):
        """Source frame shown at output frame o outside transitions, or None."""
        for sgm in self.plan["segments"]:
            lo = sgm["out_start_frame"] + sgm.get("trans_in_frames", 0)
            hi = sgm["out_end_frame"] - sgm.get("trans_out_frames", 0)
            if lo <= o < hi:
                return sgm["src_start_frame"] + (o - sgm["out_start_frame"])
        return None

    def mad(self, a, b):
        return core.mad(a, b)

    def src_vf(self, out_frame, captions=True):
        """Source-frame filter: geometry + colour, plus captions as of out_frame."""
        vf = self.base_vf
        if captions and self.cap_filter:
            vf += ",setpts=%.9f/TB,%s" % (
                float(core.frame_time(out_frame, self.fps)),
                self.cap_filter,
            )
        return vf


def _grab(path, first, count, fps, vf=None, size=None):
    tw, th = size or (core.THUMB_W, core.THUMB_H)
    if vf is None:
        return core.grab_frames(path, first, count, fps, size=size)
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
        "%s,scale=%d:%d:flags=area,format=gray" % (vf, tw, th),
        "-f",
        "rawvideo",
        "-",
    ]
    raw = core.run(cmd).stdout
    n = tw * th
    return [raw[i : i + n] for i in range(0, len(raw) - n + 1, n)]


def _match(ctx, out_thumb, center, out_frame, radius=2):
    """Offset (-radius..radius) of the source frame best matching out_thumb."""
    first = max(0, center - radius)
    last = min(ctx.plan["source_frames"] - 1, center + radius)
    thumbs = _grab(
        ctx.plan["source"], first, last - first + 1, ctx.fps, ctx.src_vf(out_frame)
    )
    scored = sorted(
        (ctx.mad(out_thumb, t), first + i - center) for i, t in enumerate(thumbs)
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
        # Centre the window in the piece's clean part (clear of transitions).
        t_in = core.frame_time(s.get("trans_in_frames", 0), fps)
        t_out = core.frame_time(s.get("trans_out_frames", 0), fps)
        clean = Fraction(s["frames"]) / fps - t_in - t_out
        if clean < AUDIO_WINDOW + Fraction(1, 10):
            continue
        mid = t_in + (clean - AUDIO_WINDOW) / 2
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


def verify_cuts(ctx, out_path, max_cuts=50):
    """Match the first and last *clean* frame of every piece against the source."""
    probes = []  # (label, out_frame, src_frame)
    for s in ctx.plan["segments"]:
        first = s["out_start_frame"] + s.get("trans_in_frames", 0)
        last = s["out_end_frame"] - 1 - s.get("trans_out_frames", 0)
        for label, of in (
            ("start of piece %d" % s["index"], first),
            ("end of piece %d" % s["index"], last),
        ):
            if of in ctx.busy:
                continue  # an effect covers it; EFFECTS checks that frame instead
            probes.append((label, of, ctx.src_frame(of)))
    if max_cuts and len(probes) > max_cuts * 2:
        step = len(probes) / float(max_cuts * 2)
        probes = [probes[int(i * step)] for i in range(max_cuts * 2)]
    results = []
    for label, of, sf in probes:
        thumbs = core.grab_frames(out_path, of, 1, ctx.fps)
        if not thumbs or sf is None:
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
        m = _match(ctx, thumbs[0], sf, of)
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


def _thumb_out(ctx, out_path, o, size=None):
    t = core.grab_frames(out_path, o, 1, ctx.fps, size=size)
    return t[0] if t else None


def _thumb_src(ctx, sf, out_frame, effect=None, captions=True, size=None):
    vf = ctx.src_vf(out_frame, captions)
    if effect is not None:
        vf = "%s[g];%s;[x]null" % (
            vf,
            fx.local_effect_graph(effect, ctx.size, "g", "x", ctx.tmp, enable=False),
        )
    t = _grab(ctx.plan["source"], sf, 1, ctx.fps, vf, size)
    return t[0] if t else None


def verify_effects(ctx, out_path):
    """Each local effect is present on its first and last frame, absent just outside."""
    rows = []
    total = ctx.plan["total_frames"]
    local = [e for e in ctx.effects if "out_start_frame" in e]
    for e in local:
        a, b = e["out_start_frame"], e["out_end_frame"]
        for o, inside in ((a, True), (b - 1, True), (a - 1, False), (b, False)):
            if o < 0 or o >= total:
                continue
            others = [
                x
                for x in local
                if x is not e and x["out_start_frame"] <= o < x["out_end_frame"]
            ]
            sf = ctx.src_frame(o)
            reason = ctx.busy.get(o) if not inside else None
            if (
                sf is None
                or others
                or (reason and not reason.endswith("effect"))
                or (not inside and reason)
            ):
                rows.append(
                    {
                        "effect": e["type"],
                        "index": e["index"],
                        "out_frame": o,
                        "expect": "on" if inside else "off",
                        "status": "SKIP",
                    }
                )
                continue
            size = TEXT_THUMB if e["type"] == "text" else None
            out_t = _thumb_out(ctx, out_path, o, size)
            plain = _thumb_src(ctx, sf, o, size=size)
            with_fx = _thumb_src(ctx, sf, o, e, size=size)
            d_fx, d_plain = ctx.mad(out_t, with_fx), ctx.mad(out_t, plain)
            near, far = (d_fx, d_plain) if inside else (d_plain, d_fx)
            if abs(d_fx - d_plain) < 0.5:
                status = "AMBIGUOUS"
            else:
                status = (
                    "PASS" if near <= MATCH_MAX_MAD and near + 0.5 < far else "FAIL"
                )
            rows.append(
                {
                    "effect": e["type"],
                    "index": e["index"],
                    "out_frame": o,
                    "expect": "on" if inside else "off",
                    "status": status,
                    "mad_with_effect": round(d_fx, 2),
                    "mad_without": round(d_plain, 2),
                }
            )
    return rows


def _expected_transition_frame(ctx, prev, cur, k):
    """Re-create transition frame k by running the same xfade on the source pieces."""
    ffmpeg, _ = core.require_ffmpeg()
    fps, d = ctx.fps, cur["trans_in_frames"]
    a0 = prev["src_start_frame"] + (cur["out_start_frame"] - prev["out_start_frame"])
    b0 = cur["src_start_frame"]
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    for f0 in (a0, b0):
        if f0:
            cmd += ["-ss", "%.9f" % float((Fraction(f0) - Fraction(1, 2)) / fps)]
        cmd += ["-i", ctx.plan["source"]]
    piece = "trim=end_frame=%d,settb=%d/%d,setpts=N,%s,format=yuv420p" % (
        d + 1,
        fps.denominator,
        fps.numerator,
        ",".join(ctx.geo),
    )
    post = list(ctx.color)
    if ctx.cap_filter:
        m = cur["out_start_frame"] + k
        post += ["setpts=%.9f/TB" % float(core.frame_time(m, fps)), ctx.cap_filter]
    graph = (
        "[0:v:0]%s[a];[1:v:0]%s[b];[a][b]xfade=transition=%s:duration=%.9f:offset=0,"
        "select='eq(n\\,%d)',%sscale=%d:%d:flags=area,format=gray[o]"
        % (
            piece,
            piece,
            cur["trans_in_type"],
            float(core.frame_time(d, fps)),
            k,
            "".join(f + "," for f in post),
            core.THUMB_W,
            core.THUMB_H,
        )
    )
    cmd += [
        "-filter_complex",
        graph,
        "-map",
        "[o]",
        "-fps_mode",
        "passthrough",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-",
    ]
    raw = core.run(cmd).stdout
    size = core.THUMB_W * core.THUMB_H
    return raw[:size] if len(raw) >= size else None


def verify_transitions(ctx, out_path):
    """Mid-transition frames must match the same transition rebuilt from the source."""
    rows = []
    segs = ctx.plan["segments"]
    for i, s in enumerate(segs):
        d = s.get("trans_in_frames", 0)
        if not d or i == 0:
            continue
        prev = segs[i - 1]
        k = d // 2
        m = s["out_start_frame"] + k
        a_src = prev["src_start_frame"] + (m - prev["out_start_frame"])
        b_src = s["src_start_frame"] + k
        out_t = _thumb_out(ctx, out_path, m)
        expected = _expected_transition_frame(ctx, prev, s, k)
        ta, tb = _thumb_src(ctx, a_src, m), _thumb_src(ctx, b_src, m)
        de, da, db = ctx.mad(out_t, expected), ctx.mad(out_t, ta), ctx.mad(out_t, tb)
        if de > MATCH_MAX_MAD:
            status = "FAIL"
        elif de + 0.5 < min(da, db):
            status = "PASS"  # matches the rebuilt transition better than either piece
        else:
            status = (
                "AMBIGUOUS"
                if ctx.mad(ta, tb) < 3
                or ctx.mad(expected, ta) < 1
                or ctx.mad(expected, tb) < 1
                else "FAIL"
            )
        rows.append(
            {
                "into_piece": i,
                "type": s["trans_in_type"],
                "frames": d,
                "mid_frame": m,
                "status": status,
                "mad_vs_expected": round(de, 2),
                "mad_vs_outgoing": round(da, 2),
                "mad_vs_incoming": round(db, 2),
            }
        )
    return rows


def verify_fades(ctx, out_path):
    rows = []
    total = ctx.plan["total_frames"]
    for e in ctx.effects:
        if e["type"] not in ("fade_in", "fade_out"):
            continue
        o = 0 if e["type"] == "fade_in" else total - 1
        t = _thumb_out(ctx, out_path, o)
        mean = sum(t) / len(t) if t else 255
        rows.append(
            {
                "effect": e["type"],
                "frame": o,
                "mean_luma": round(mean, 1),
                "status": "PASS" if mean <= 24 else "FAIL",
            }
        )
    return rows


def verify_captions(ctx, out_path, max_cues=6):
    """Burned captions: right text at the right output time, nothing between."""
    if not ctx.cap_filter:
        return []
    cap = next(e for e in ctx.effects if e["type"] == "captions")
    cues = fx.build_caption_cues(ctx.plan, cap)
    rows = []
    picks = (
        cues
        if len(cues) <= max_cues
        else [cues[int(i * len(cues) / max_cues)] for i in range(max_cues)]
    )
    probes = [(core.frame_of((a + b) / 2, ctx.fps), True, body) for a, b, body in picks]
    for (a1, b1, _), (a2, _b2, _) in zip(cues, cues[1:]):
        if a2 - b1 > 3 / ctx.fps:
            probes.append((core.frame_of((b1 + a2) / 2, ctx.fps), False, ""))
            break
    for o, expect_text, body in probes:
        sf = ctx.src_frame(o)
        if sf is None or o in ctx.busy:
            continue
        out_t = _thumb_out(ctx, out_path, o, TEXT_THUMB)
        d_cap = ctx.mad(out_t, _thumb_src(ctx, sf, o, size=TEXT_THUMB))
        d_plain = ctx.mad(
            out_t, _thumb_src(ctx, sf, o, captions=False, size=TEXT_THUMB)
        )
        if d_cap > MATCH_MAX_MAD:
            status = "FAIL"
        elif not expect_text:
            status = "PASS" if abs(d_cap - d_plain) < 0.5 else "FAIL"
        elif d_cap + 0.5 < d_plain:
            status = "PASS"
        elif d_plain + 0.5 < d_cap:
            status = "FAIL"  # looks uncaptioned
        else:
            status = "AMBIGUOUS"  # text too small to separate at this resolution
        rows.append(
            {
                "out_frame": o,
                "expect": "text" if expect_text else "clear",
                "mad_with_captions": round(d_cap, 2),
                "mad_without": round(d_plain, 2),
                "status": status,
                "cue": body[:40],
            }
        )
    return rows


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

    cut_rows, fx_rows = [], {}
    if plan["has_video"] and v and max_cuts != 0:
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(plan, tmp)
            cut_rows = verify_cuts(ctx, out_path, max_cuts)
            fails = [r for r in cut_rows if r["status"] == "FAIL"]
            amb = [r for r in cut_rows if r["status"] == "AMBIGUOUS"]
            _check(
                rows,
                "CUT_FRAMES",
                not fails,
                "%d boundaries checked: %d exact, %d ambiguous (static shot), "
                "%d wrong%s"
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
            for code, fn, what in (
                ("TRANSITIONS", verify_transitions, "transitions blended"),
                ("EFFECTS", verify_effects, "effect edge frames"),
                ("CAPTIONS", verify_captions, "caption probes"),
                ("FADES", verify_fades, "fade frames"),
            ):
                got = fn(ctx, out_path)
                if not got:
                    continue
                fx_rows[code.lower()] = got
                bad = [r for r in got if r["status"] == "FAIL"]
                amb = sum(1 for r in got if r["status"] == "AMBIGUOUS")
                skip = sum(1 for r in got if r["status"] == "SKIP")
                _check(
                    rows,
                    code,
                    not bad,
                    "%d %s: %d ok, %d ambiguous, %d skipped, %d wrong%s"
                    % (
                        len(got),
                        what,
                        len(got) - len(bad) - amb - skip,
                        amb,
                        skip,
                        len(bad),
                        ""
                        if not bad
                        else "; first: %s"
                        % {k: v2 for k, v2 in bad[0].items() if k != "status"},
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
        **fx_rows,
        "failed": [r["check"] for r in failed],
    }
