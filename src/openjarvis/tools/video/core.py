"""Exact-time core for the video editing tools.

Every time value is a ``fractions.Fraction`` of seconds, never a float: 29.97
fps is exactly 30000/1001, and frame ``n`` of a constant-frame-rate stream
starts at ``n / fps``. ffmpeg/ffprobe are driven through ``subprocess``.

Ported from claude-skills ``marketing/youtube-editor`` (MIT).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from fractions import Fraction

# Exit codes shared by every tool in the package.
EXIT_OK = 0
EXIT_FINDINGS = 1  # verifier FAIL / analyzer found nothing usable
EXIT_WARN = 2  # plan is renderable but carries warnings
EXIT_BLOCKED = 3  # plan is invalid; refuse to render
EXIT_USAGE = 4  # bad arguments or unreadable input
EXIT_NO_FFMPEG = 6  # ffmpeg/ffprobe missing


class ToolError(Exception):
    """Raised for user-facing failures; carries an exit code."""

    def __init__(self, message, code=EXIT_USAGE):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# ffmpeg plumbing
# --------------------------------------------------------------------------


def require_ffmpeg():
    """Return (ffmpeg, ffprobe) paths or raise ToolError(EXIT_NO_FFMPEG)."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ToolError(
            "ffmpeg and ffprobe are required (open source, LGPL/GPL). Install: "
            "macOS `brew install ffmpeg`, Debian/Ubuntu `sudo apt install ffmpeg`, "
            "Windows `winget install ffmpeg`.",
            EXIT_NO_FFMPEG,
        )
    return ffmpeg, ffprobe


def run(cmd, capture=True, check=True, input_bytes=None):
    """Run a command; return CompletedProcess. Raise ToolError on failure."""
    proc = subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and proc.returncode != 0:
        tail = (
            (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-12:]
        )
        raise ToolError(
            "command failed (%d): %s\n%s"
            % (proc.returncode, " ".join(cmd[:6]) + " ...", "\n".join(tail)),
            EXIT_FINDINGS,
        )
    return proc


def ffmpeg_version():
    ffmpeg, _ = require_ffmpeg()
    out = run([ffmpeg, "-hide_banner", "-version"]).stdout.decode("utf-8", "replace")
    return out.splitlines()[0] if out else "unknown"


# --------------------------------------------------------------------------
# Exact time arithmetic
# --------------------------------------------------------------------------

_TC_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")
_SMPTE_RE = re.compile(r"^(\d+):(\d+):(\d+)[:;](\d+)$")


def to_fraction(value):
    """Parse a decimal string/number into an exact Fraction (no float hop)."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        # Floats arrive from JSON; go through repr so 1.2 stays 6/5, not 1.19999...
        return Fraction(repr(value))
    text = str(value).strip()
    if "/" in text:
        num, den = text.split("/", 1)
        den_f = Fraction(den)
        if den_f == 0:
            raise ToolError("zero denominator in %r" % value)
        return Fraction(num) / den_f
    return Fraction(text)


def parse_time(value, fps=None):
    """Parse a time spec into exact Fraction seconds.

    Accepted: 12.5 · "12.5" · "1:02.250" (MM:SS.mmm) · "00:01:02.250"
    (HH:MM:SS.mmm) · "00:01:02:15" (SMPTE HH:MM:SS:FF, needs fps) · "1875f"
    (absolute frame number, needs fps).
    """
    if isinstance(value, (int, float, Fraction)):
        result = to_fraction(value)
    else:
        text = str(value).strip()
        m_smpte = _SMPTE_RE.match(text)
        m_tc = _TC_RE.match(text)
        if text.endswith("f") and text[:-1].isdigit():
            if fps is None:
                raise ToolError("frame time %r needs a known fps" % text)
            result = Fraction(int(text[:-1])) / fps
        elif m_smpte:
            if fps is None:
                raise ToolError("SMPTE timecode %r needs a known fps" % text)
            h, m, s, f = (int(x) for x in m_smpte.groups())
            nominal = round(fps)
            if f >= nominal:
                raise ToolError(
                    "frame field %d >= nominal fps %d in %r" % (f, nominal, text)
                )
            # SMPTE counts frames, not seconds: HH:MM:SS:FF at 29.97 is a label on
            # frame number N, and N / fps is the true time.
            frame_no = (h * 3600 + m * 60 + s) * nominal + f
            if ";" in text and nominal in (30, 60) and fps.denominator == 1001:
                # Drop-frame: skip 2 (or 4) labels each minute except every tenth.
                drop = 2 if nominal == 30 else 4
                minutes = h * 60 + m
                frame_no -= drop * (minutes - minutes // 10)
            result = Fraction(frame_no) / fps
        elif m_tc:
            h = int(m_tc.group(1) or 0)
            m = int(m_tc.group(2))
            s = Fraction(m_tc.group(3))
            if m >= 60 and m_tc.group(1) is not None:
                raise ToolError("minutes field >= 60 in %r" % text)
            result = Fraction(h * 3600 + m * 60) + s
        else:
            try:
                result = to_fraction(text)
            except (ValueError, ZeroDivisionError):
                raise ToolError("unparseable time %r" % value)
    if result < 0:
        raise ToolError("negative time %r" % value)
    return result


def frame_of(t, fps, mode="nearest"):
    """Map exact seconds -> frame boundary index. mode: nearest|floor|ceil."""
    x = Fraction(t) * fps
    if mode == "floor":
        return math.floor(x)
    if mode == "ceil":
        return math.ceil(x)
    # nearest, ties away from zero (deterministic, no banker's rounding)
    return math.floor(x + Fraction(1, 2))


def frame_time(n, fps):
    return Fraction(n) / fps


def fmt_ts(t, places=3):
    """Exact seconds -> HH:MM:SS.mmm (rounded half-up at `places`)."""
    t = Fraction(t)
    scale = 10**places
    total = math.floor(t * scale + Fraction(1, 2))
    whole, frac = divmod(total, scale)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if places:
        return "%02d:%02d:%02d.%0*d" % (h, m, s, places, frac)
    return "%02d:%02d:%02d" % (h, m, s)


def fmt_chapter(t):
    """YouTube chapter stamp: M:SS under an hour, H:MM:SS above (floored)."""
    whole = math.floor(Fraction(t))
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


def fmt_srt(t):
    return fmt_ts(t, 3).replace(".", ",")


def fsec(t, places=6):
    """Fraction -> float rounded for JSON display only (never fed back into math)."""
    return round(float(t), places)


def frac_str(t):
    """Fraction -> 'num/den' string: lossless JSON round-trip."""
    t = Fraction(t)
    return (
        str(t.numerator)
        if t.denominator == 1
        else "%d/%d" % (t.numerator, t.denominator)
    )


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


def _rate(text):
    try:
        r = to_fraction(text or "0/0")
    except ToolError:
        return Fraction(0)
    return r


def probe(path):
    """Return a normalized description of a media file (exact rationals kept)."""
    if not os.path.isfile(path):
        raise ToolError("input not found: %s" % path)
    _, ffprobe = require_ffmpeg()
    out = run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", path]
    ).stdout
    data = json.loads(out.decode("utf-8", "replace") or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info = {
        "path": os.path.abspath(path),
        "container": fmt.get("format_name"),
        "duration": frac_str(to_fraction(fmt.get("duration", "0"))),
        "size_bytes": int(fmt.get("size", 0) or 0),
        "video": None,
        "audio": None,
        "warnings": [],
    }
    if video:
        r_rate = _rate(video.get("r_frame_rate"))
        avg_rate = _rate(video.get("avg_frame_rate"))
        fps = r_rate if r_rate else avg_rate
        vfr = bool(
            r_rate and avg_rate and abs(r_rate - avg_rate) / r_rate > Fraction(1, 200)
        )
        v_start = to_fraction(video.get("start_time", "0") or "0")
        v_dur = to_fraction(video.get("duration", fmt.get("duration", "0")) or "0")
        info["video"] = {
            "index": video.get("index"),
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": video.get("width"),
            "height": video.get("height"),
            "pix_fmt": video.get("pix_fmt"),
            "fps": frac_str(fps),
            "fps_display": fsec(fps, 3) if fps else None,
            "avg_frame_rate": frac_str(avg_rate),
            "vfr_suspected": vfr,
            "start_time": frac_str(v_start),
            "duration": frac_str(v_dur),
            "frames_estimate": frame_of(v_dur, fps) if fps else None,
            "color_space": video.get("color_space"),
            "color_transfer": video.get("color_transfer"),
            "color_primaries": video.get("color_primaries"),
            "field_order": video.get("field_order"),
            "rotation": _rotation(video),
        }
        if vfr:
            info["warnings"].append(
                "VFR_SUSPECTED: r_frame_rate %s differs from avg_frame_rate %s; frame "
                "numbers do not map linearly to time. The renderer conforms to CFR "
                "first." % (video.get("r_frame_rate"), video.get("avg_frame_rate"))
            )
        if video.get("field_order") not in (None, "progressive", "unknown"):
            info["warnings"].append(
                "INTERLACED: field_order=%s. YouTube wants progressive; render with "
                "--deinterlace." % video.get("field_order")
            )
    if audio:
        a_start = to_fraction(audio.get("start_time", "0") or "0")
        info["audio"] = {
            "index": audio.get("index"),
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio.get("sample_rate", 0) or 0),
            "channels": audio.get("channels"),
            "channel_layout": audio.get("channel_layout"),
            "start_time": frac_str(a_start),
            "duration": frac_str(
                to_fraction(audio.get("duration", fmt.get("duration", "0")) or "0")
            ),
        }
        if video:
            offset = a_start - to_fraction(info["video"]["start_time"])
            info["audio"]["offset_vs_video"] = frac_str(offset)
            if offset != 0:
                info["warnings"].append(
                    "AV_START_OFFSET: audio starts %+.6fs relative to video; the "
                    "renderer compensates sample-exactly." % float(offset)
                )
    if not video:
        info["warnings"].append(
            "NO_VIDEO: audio-only input; frame snapping uses 1 ms ticks."
        )
    return info


def _rotation(stream):
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return side["rotation"]
    tags = stream.get("tags") or {}
    return int(tags["rotate"]) if "rotate" in tags else 0


def fps_of(info):
    """Exact frame rate of a probe() result (1000 'ticks' for audio-only)."""
    if info.get("video") and info["video"]["fps"] not in ("0", None):
        return to_fraction(info["video"]["fps"])
    return Fraction(1000)


def count_frames(path):
    """Exact video frame count by counting packets (fast, no decode)."""
    _, ffprobe = require_ffmpeg()
    out = run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_packets",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            path,
        ]
    ).stdout
    text = out.decode().strip().split(",")[0]
    return int(text) if text.isdigit() else None


def keyframes(path, limit=100000):
    """Keyframe timestamps (seconds) — shows why stream-copy cuts are imprecise."""
    _, ffprobe = require_ffmpeg()
    out = run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            path,
        ]
    ).stdout
    times = []
    for line in out.decode().splitlines():
        line = line.strip().strip(",")
        if line and line != "N/A":
            times.append(to_fraction(line))
            if len(times) >= limit:
                break
    return times


def measure_loudness(path):
    """EBU R128 integrated loudness, true peak and LRA of the first audio stream."""
    ffmpeg, _ = require_ffmpeg()
    err = run(
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


# --------------------------------------------------------------------------
# Raw frame access (for boundary verification)
# --------------------------------------------------------------------------

THUMB_W, THUMB_H = 64, 36


def grab_frames(path, first_frame, count, fps, start_time=Fraction(0), size=None):
    """Decode `count` frames starting at frame index `first_frame` as tiny gray thumbs.

    Input seeking lands half a frame before the target so ffmpeg's accurate
    seek emits exactly frame `first_frame` first, regardless of keyframes.
    Returns a list of bytes objects (THUMB_W*THUMB_H each, or size[0]*size[1]).
    """
    tw, th = size or (THUMB_W, THUMB_H)
    ffmpeg, _ = require_ffmpeg()
    first_frame = max(0, first_frame)
    seek = (
        Fraction(0)
        if first_frame == 0
        else (Fraction(first_frame) - Fraction(1, 2)) / fps
    )
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
        "scale=%d:%d:flags=area,format=gray" % (tw, th),
        "-f",
        "rawvideo",
        "-",
    ]
    raw = run(cmd).stdout
    n = tw * th
    return [raw[i : i + n] for i in range(0, len(raw) - n + 1, n)]


def mad(a, b):
    """Mean absolute difference between two equal-size gray thumbs (0-255)."""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


# --------------------------------------------------------------------------
# JSON / output helpers
# --------------------------------------------------------------------------


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ToolError("file not found: %s" % path)
    except json.JSONDecodeError as exc:
        raise ToolError("invalid JSON in %s: %s" % (path, exc))


def write_json(obj, path=None):
    text = json.dumps(obj, indent=2, ensure_ascii=False)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return text


def load_words(path):
    """Load word-level timestamps.

    Accepts: a list of {word|text, start, end}; whisper/faster-whisper JSON
    ({"segments": [{"words": [...]}]}); or WhisperX ({"word_segments": [...]}).
    Returns [{"word", "start": Fraction, "end": Fraction}] sorted by start.
    """
    data = load_json(path)
    items = []
    if isinstance(data, dict):
        if "word_segments" in data:
            items = data["word_segments"]
        elif "segments" in data:
            for seg in data["segments"]:
                items.extend(seg.get("words", []) or [])
        elif "words" in data:
            items = data["words"]
    elif isinstance(data, list):
        items = data
    words = []
    for w in items:
        text = (w.get("word") if "word" in w else w.get("text", "")) or ""
        if "start" not in w or "end" not in w or w["start"] is None or w["end"] is None:
            continue
        start, end = to_fraction(w["start"]), to_fraction(w["end"])
        if end < start:
            start, end = end, start
        words.append({"word": text.strip(), "start": start, "end": end})
    words.sort(key=lambda x: x["start"])
    if not words:
        raise ToolError(
            "no word-level timestamps found in %s (need start/end per word)" % path
        )
    return words
