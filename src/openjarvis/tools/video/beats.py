"""Find the beats in a music track so cuts and hits can land on them.

Ported from claude-skills marketing/youtube-editor (beat_detector.py).
"""

from __future__ import annotations

import math
import os
import sys
from array import array

from openjarvis.tools.video import core

RATE = 11025
HOP = 110  # samples per hop: an onset envelope at ~100 Hz
HOP_S = HOP / float(RATE)
FMT = "aformat=sample_fmts=fltp:channel_layouts=mono"
BANDS = (
    "asplit=3[a][b][c];[a]lowpass=f=150,%s[l];[b]highpass=f=150,lowpass=f=4000,%s[m];"
    "[c]highpass=f=4000,%s[h];[l][m][h]amerge=inputs=3" % (FMT, FMT, FMT)
)


def band_energies(path, start=0.0, duration=None):
    """Per-band energy per 10 ms hop: three lists of floats."""
    ffmpeg, _ = core.require_ffmpeg()
    cmd = [ffmpeg, "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", "%.6f" % start]
    if duration:
        cmd += ["-t", "%.6f" % duration]
    cmd += [
        "-i",
        path,
        "-vn",
        "-filter_complex",
        "[0:a:0]aformat=channel_layouts=mono,aresample=%d,%s" % (RATE, BANDS),
        "-ac",
        "3",
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    raw = core.run(cmd).stdout
    pcm = array("h")
    pcm.frombytes(raw[: len(raw) // 6 * 6])
    if sys.byteorder == "big":
        pcm.byteswap()
    hop = HOP
    n = len(pcm) // 3 // hop
    out = []
    for band in range(3):
        ch = pcm[band::3]
        e = []
        for i in range(n):
            seg = ch[i * hop : (i + 1) * hop]
            e.append(sum(v * v for v in seg) / hop)
        out.append(e)
    return out


def onset_envelope(energies):
    """Half-wave-rectified rise in log energy, summed over bands, mean-removed."""
    n = len(energies[0])
    env = [0.0] * n
    for e in energies:
        lg = [math.log1p(v / 1e4) for v in e]
        for i in range(1, n):
            d = lg[i] - lg[i - 1]
            if d > 0:
                env[i] += d
    # subtract a local mean (1 s) so loud sections do not swamp quiet ones
    w = int(1.0 / HOP_S)
    pre = [0.0]
    for v in env:
        pre.append(pre[-1] + v)
    out = []
    for i in range(n):
        a, b = max(0, i - w // 2), min(n, i + w // 2)
        m = (pre[b] - pre[a]) / max(1, b - a)
        out.append(max(0.0, env[i] - m))
    return out


def estimate_tempo(env, bpm_hint=None, lo=60.0, hi=190.0):
    """Best beat period in hops, via autocorrelation weighted by a log-Gaussian
    prior on 120 BPM (or a narrow one on the hint)."""
    n = len(env)
    mean = sum(env) / max(1, n)
    x = [v - mean for v in env]
    centre, width = (bpm_hint, 0.05) if bpm_hint else (120.0, 0.9)
    scores = []
    min_lag = int(60.0 / hi / HOP_S)
    max_lag = int(60.0 / lo / HOP_S)
    energy = sum(v * v for v in x) or 1.0
    for lag in range(min_lag, max_lag + 1):
        ac = sum(x[i] * x[i + lag] for i in range(n - lag)) / energy
        bpm = 60.0 / (lag * HOP_S)
        prior = math.exp(-0.5 * (math.log2(bpm / centre) / width) ** 2)
        scores.append((ac * prior, lag, ac))
    scores.sort(reverse=True)
    _, lag, ac = scores[0]
    # parabolic refinement of the period
    by_lag = {s[1]: s[0] for s in scores}
    y0, y1, y2 = by_lag.get(lag - 1, 0), by_lag[lag], by_lag.get(lag + 1, 0)
    den = y0 - 2 * y1 + y2
    period = lag + (0.5 * (y0 - y2) / den if den else 0.0)
    rest = [s[0] for s in scores[1:] if abs(s[1] - lag) > 2]
    contrast = (scores[0][0] - (sum(rest) / len(rest) if rest else 0)) / (
        abs(scores[0][0]) or 1
    )
    conf = max(0.0, min(1.0, ac * 2)) * max(0.0, min(1.0, contrast))
    return period, conf


def grid_fit(env, bpm):
    """How strongly a beat grid at this tempo (best phase) lands on onsets,
    relative to the envelope's mean. A wrong metre lands between the hits."""
    n = len(env)
    mean = sum(env) / max(1, n) or 1.0
    period = 60.0 / bpm / HOP_S
    best = 0.0
    for ph in range(int(period)):
        idx = [int(round(ph + k * period)) for k in range(int((n - ph) / period))]
        if idx:
            v = sum(max(env[max(0, i - 1) : i + 2]) for i in idx) / len(idx)
            best = max(best, v)
    return best / mean


RATIOS = (0.5, 2 / 3.0, 0.75, 1.0, 4 / 3.0, 1.5, 2.0)


def rerank(env, bpm, width=0.9):
    """Autocorrelation confuses a tempo with its metrical relatives (x2/3, x3/4,
    x3/2 ...). Score each relative by grid fit under the 120 BPM prior and keep
    the best; return the alternatives so a human can overrule by ear."""
    cands = []
    for r in RATIOS:
        c = bpm * r
        if 60 <= c <= 190:
            fit = grid_fit(env, c)
            prior = math.exp(-0.5 * (math.log2(c / 120.0) / width) ** 2)
            cands.append((fit * prior, c, fit))
    cands.sort(reverse=True)
    return cands[0][1], [
        {"bpm": round(c, 2), "grid_fit": round(f, 2)} for _, c, f in cands
    ]


def track_beats(env, period, tightness=100.0):
    """Dynamic-programming beat tracker: maximise onset strength at the beats
    while penalising intervals that stray from the period."""
    n = len(env)
    score = list(env)
    back = [-1] * n
    lo, hi = int(round(period / 2)), int(round(period * 2))
    for t in range(n):
        best, arg = -1e18, -1
        for p in range(max(0, t - hi), t - lo + 1):
            if p < 0:
                continue
            pen = tightness * (math.log((t - p) / period)) ** 2
            v = score[p] - pen
            if v > best:
                best, arg = v, p
        if arg >= 0 and best > 0:
            score[t] = env[t] + best
            back[t] = arg
    # start from the best-scoring position in the last period
    tail = range(max(0, n - int(period)), n)
    t = max(tail, key=lambda i: score[i])
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats.reverse()
    return beats


def refine(env, idx):
    """Onset position to a fraction of a hop. An onset part-way through hop k puts
    part of its rise in k and the rest in k+1, so the split says where it fell."""
    a, b = max(0, idx - 3), min(len(env) - 1, idx + 3)
    k = max(range(a, b + 1), key=lambda i: env[i])
    if k > 0 and env[k - 1] > (env[k + 1] if k + 1 < len(env) else 0):
        k -= 1
    nxt = env[k + 1] if k + 1 < len(env) else 0.0
    tot = env[k] + nxt
    return k + (nxt / tot if tot > 0 else 0.0)


def downbeat_phase(times, kick, meter=4):
    """The beat phase (of 4) whose beats carry the most low-band energy."""
    sums = [0.0] * meter
    for j, t in enumerate(times):
        i = int(t / HOP_S)
        sums[j % meter] += max(kick[i : i + 6] or [0.0])
    return max(range(meter), key=lambda k: sums[k])


def extend_back(env, idx, period):
    """The tracker can miss beats before its first pick (no rise at hop 0): add
    them back where the envelope shows an onset near the expected position."""
    strong = sorted(env[i] for i in idx)[len(idx) // 2] if idx else 0
    first = idx[0] if idx else 0
    t = first - period
    while t >= -1:
        c = max(0, int(round(t)))
        win = range(max(0, c - 2), min(len(env), c + 3))
        k = max(win, key=lambda i: env[i])
        if env[k] < 0.3 * strong and c > 1:
            break
        idx.insert(0, k if env[k] >= 0.3 * strong else c)
        t -= period
    return idx


def sections(energies, beats_t, window_beats=8):
    """Average loudness per 8-beat block; flags drops (-40%) and rises (+40%)."""
    if len(beats_t) < window_beats * 2:
        return []
    total = [sum(e[i] for e in energies) for i in range(len(energies[0]))]
    out, prev = [], None
    for k in range(0, len(beats_t) - window_beats, window_beats):
        a = int(beats_t[k] / HOP_S)
        b = int(beats_t[k + window_beats] / HOP_S)
        level = 10 * math.log10(sum(total[a:b]) / max(1, b - a) + 1e-9)
        kind = None
        if prev is not None and level - prev > 3:
            kind = "rise"
        elif prev is not None and prev - level > 3:
            kind = "drop"
        out.append(
            {"start": round(beats_t[k], 4), "level_db": round(level, 1), "change": kind}
        )
        prev = level
    return out


def detect(path, start=0.0, duration=None, bpm=None):
    energies = band_energies(path, start, duration)
    if not energies[0] or len(energies[0]) < 300:
        raise core.ToolError("need at least 3 seconds of audio to find a beat")
    env = onset_envelope(energies)
    period, conf = estimate_tempo(env, bpm)
    alternatives = []
    if bpm:
        period = 60.0 / bpm / HOP_S
    else:
        best, alternatives = rerank(env, 60.0 / (period * HOP_S))
        period = 60.0 / best / HOP_S
    idx = extend_back(env, track_beats(env, period), period)
    rel = [refine(env, i) * HOP_S for i in idx]
    phase = downbeat_phase(rel, energies[0])
    times = [round(t + start, 4) for t in rel]
    rep = {
        "file": os.path.basename(path),
        "start": start,
        "tempo_bpm": round(60.0 / (period * HOP_S), 2),
        "confidence": round(conf, 2),
        "beat_count": len(times),
        "beats": times,
        "downbeats": times[phase::4],
        "meter": "4/4 (assumed)",
        "sections": sections(energies, [t - start for t in times]),
        "precision_ms": round(HOP_S * 500, 1),
        "tempo_alternatives": alternatives,
        "tempo_source": "given" if bpm else "detected",
    }
    if conf < 0.3:
        rep["warning"] = "weak or irregular beat: listen before cutting to it"
    return rep
