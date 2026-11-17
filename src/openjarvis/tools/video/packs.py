"""Music, sound effects and colour grades for an edit, licence-clean.

Ported from claude-skills marketing/youtube-editor (asset_packs.py).
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import tempfile

from openjarvis.tools.video import core

CUBE_SIZE = 33
LICENCE_OPENFILM = "CC0 1.0 (OpenFilm library; free for any use, no credit needed)"
LICENCE_BUILTIN = "made by code in this skill (no third-party rights)"


def _curve(points):
    """17-point curve from (x, y) control points, linear between them."""
    pts = sorted(points)
    out = []
    for i in range(17):
        x = i / 16.0
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= x <= x1:
                out.append(round(y0 + (y1 - y0) * (x - x0) / (x1 - x0 or 1), 4))
                break
    return out


S_CURVE = _curve([(0, 0.02), (0.25, 0.2), (0.5, 0.5), (0.75, 0.8), (1, 0.98)])
BUILTIN_GRADES = {
    # name: (description, grade in OpenFilm's format)
    "teal-orange": (
        "blockbuster contrast: warm skin, teal shadows (motivation, hype)",
        {
            "sat": 1.08,
            "all": S_CURVE,
            "r": _curve([(0, 0), (0.5, 0.52), (1, 1)]),
            "b": _curve([(0, 0.08), (0.5, 0.48), (1, 0.92)]),
        },
    ),
    "moody": (
        "lifted blacks, rolled-off highs, low saturation (storytelling, night)",
        {
            "sat": 0.78,
            "all": _curve([(0, 0.07), (0.5, 0.47), (1, 0.9)]),
            "b": _curve([(0, 0.04), (0.5, 0.52), (1, 1)]),
        },
    ),
    "warm-vlog": (
        "sunny, soft, slightly warm (day in my life, lifestyle)",
        {
            "mix": [1.05, 0, 0, 0.01, 0, 1.01, 0, 0, 0, 0, 0.93, 0],
            "sat": 1.06,
            "all": _curve([(0, 0.03), (0.5, 0.52), (1, 0.98)]),
        },
    ),
    "punch": (
        "clean contrast and colour pop (talking head, tutorials)",
        {"sat": 1.18, "all": S_CURVE},
    ),
    "bleach": (
        "bleach bypass: gritty, desaturated, hard contrast (intense moments)",
        {
            "sat": 0.55,
            "all": _curve([(0, 0), (0.25, 0.15), (0.5, 0.5), (0.75, 0.87), (1, 1)]),
        },
    ),
}

STYLES = {
    # what each genre pulls; the planner styles use the same names
    "motivation": {
        "grade": "teal-orange",
        "music": "epic cinematic motivational rise drums",
        "energy": 4,
        "bpm": (90, 140),
        "sfx": {
            "hit": "cinematic impact boom",
            "whoosh": "whoosh transition",
            "riser": "riser build up tension",
        },
        "look": {"grain": 6, "vignette": 0.35, "letterbox": 2.0},
    },
    "storytelling": {
        "grade": "moody",
        "music": "emotional piano documentary tension",
        "energy": 2,
        "bpm": (60, 100),
        "sfx": {
            "hit": "deep boom impact",
            "whoosh": "soft whoosh",
            "riser": "suspense riser",
        },
        "look": {"grain": 8, "vignette": 0.45},
    },
    "day-in-life": {
        "grade": "warm-vlog",
        "music": "upbeat happy lofi vlog groove",
        "energy": 3,
        "bpm": (85, 125),
        "sfx": {
            "pop": "pop",
            "whoosh": "quick whoosh swipe",
            "click": "camera shutter click",
        },
        "look": {"grain": 3, "vignette": 0.2},
    },
    "talking-head": {
        "grade": "punch",
        "music": "light upbeat background corporate",
        "energy": 2,
        "bpm": (90, 130),
        "sfx": {"pop": "pop", "whoosh": "whoosh", "ding": "ding notification"},
        "look": {},
    },
}


# ---------------------------------------------------------------- grades -> .cube
def _table(t, x):
    if not t:
        return x
    if len(t) == 1:
        return t[0]
    x = min(1.0, max(0.0, x))
    p = x * (len(t) - 1)
    i = min(int(p), len(t) - 2)
    return t[i] + (t[i + 1] - t[i]) * (p - i)


def apply_grade(rgb, g, strength=1.0):
    """One RGB triple (0..1) through a grade, as OpenFilm's SVG filter chain does it."""

    def lerp(a, b):
        return a + (b - a) * strength

    r, gr, b = rgb
    m = g.get("mix")
    if m and len(m) == 12:
        ident = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
        m = [lerp(ident[i], v) for i, v in enumerate(m)]
        r, gr, b = [
            min(
                1,
                max(
                    0,
                    m[c * 4] * r + m[c * 4 + 1] * gr + m[c * 4 + 2] * b + m[c * 4 + 3],
                ),
            )
            for c in range(3)
        ]
    s = g.get("sat")
    if s is not None and s != 1:
        s = lerp(1, s)
        r, gr, b = (
            min(
                1,
                max(
                    0,
                    (0.213 + 0.787 * s) * r
                    + (0.715 - 0.715 * s) * gr
                    + (0.072 - 0.072 * s) * b,
                ),
            ),
            min(
                1,
                max(
                    0,
                    (0.213 - 0.213 * s) * r
                    + (0.715 + 0.285 * s) * gr
                    + (0.072 - 0.072 * s) * b,
                ),
            ),
            min(
                1,
                max(
                    0,
                    (0.213 - 0.213 * s) * r
                    + (0.715 - 0.715 * s) * gr
                    + (0.072 + 0.928 * s) * b,
                ),
            ),
        )
    out = []
    for v, ch in ((r, "r"), (gr, "g"), (b, "b")):
        if g.get("all") or g.get(ch):
            v2 = _table(g.get(ch), _table(g.get("all"), v))
            v = lerp(v, v2)
        out.append(min(1.0, max(0.0, v)))
    return out


def write_cube(grade, path, title="grade", strength=1.0, size=CUBE_SIZE):
    lines = [
        'TITLE "%s"' % re.sub(r"[^\w .-]", "", title)[:60],
        "LUT_3D_SIZE %d" % size,
        "DOMAIN_MIN 0 0 0",
        "DOMAIN_MAX 1 1 1",
    ]
    n = size - 1
    for bi in range(size):  # .cube order: red changes fastest
        for gi in range(size):
            for ri in range(size):
                o = apply_grade((ri / n, gi / n, bi / n), grade, strength)
                lines.append("%.6f %.6f %.6f" % tuple(o))
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------- openfilm library
def openfilm_root():
    env = os.environ.get("OPENFILM_ROOT")
    cands = [env] if env else []
    exe = shutil.which("openfilm")
    if exe:
        real = os.path.realpath(exe)
        cands.append(os.path.dirname(os.path.dirname(real)))
    try:
        npm_root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=20
        ).stdout.strip()
        if npm_root:
            cands.append(os.path.join(npm_root, "openfilm"))
    except (OSError, subprocess.SubprocessError):
        pass
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "library", "audio.json")):
            return c
    return None


def _name(item):
    n = item.get("name")
    return n.get("en", "") if isinstance(n, dict) else str(n or item.get("id"))


def catalogue(kind):
    """Items of one kind: music | sfx | grade. Built-in grades are always included."""
    items = []
    if kind == "grade":
        for name, (desc, g) in BUILTIN_GRADES.items():
            items.append(
                {
                    "id": "grade:" + name,
                    "name": name,
                    "tags": desc.replace(",", " ").split(),
                    "grade": g,
                    "source": "built-in",
                    "licence": LICENCE_BUILTIN,
                }
            )
    root = openfilm_root()
    if not root:
        return items
    lib = os.path.join(root, "library")
    if kind in ("music", "sfx"):
        data = json.load(open(os.path.join(lib, "audio.json")))
        want = "music" if kind == "music" else "sound"
        for it in data["items"]:
            if it.get("cat") == want:
                items.append(
                    {
                        "id": "openfilm/" + it["id"],
                        "name": _name(it),
                        "tags": it.get("tags", []),
                        "group": it.get("group"),
                        "bpm": it.get("bpm"),
                        "energy": it.get("energy"),
                        "sec": round(it.get("durationMs", 0) / 1000.0, 2),
                        "source": "openfilm",
                        "licence": LICENCE_OPENFILM,
                    }
                )
    elif kind == "grade":
        data = json.load(open(os.path.join(lib, "catalogs", "filters.json")))
        for it in data["items"]:
            if it.get("grade"):
                items.append(
                    {
                        "id": "openfilm/" + it["id"],
                        "name": _name(it),
                        "tags": [it.get("group", "")] + _name(it).lower().split(),
                        "grade": it["grade"],
                        "source": "openfilm",
                        "licence": LICENCE_OPENFILM,
                    }
                )
    return items


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1]


def search(kind, prompt="", bpm=None, energy=None, min_sec=None, limit=8):
    q = set(_words(prompt))
    rows = []
    for it in catalogue(kind):
        words = set(
            _words(
                " ".join(it.get("tags", []))
                + " "
                + it["name"]
                + " "
                + str(it.get("group") or "")
            )
        )
        hit = len(q & words)
        score = hit / float(len(q)) if q else 0.5
        if q and not hit:
            continue
        if min_sec and (it.get("sec") or 0) < min_sec:
            continue
        if bpm and it.get("bpm"):
            lo, hi = bpm if isinstance(bpm, tuple) else (bpm - 8, bpm + 8)
            mid = (lo + hi) / 2.0
            score += 0.3 * math.exp(
                -(((it["bpm"] - mid) / max(8.0, (hi - lo) / 2.0)) ** 2)
            )
        if energy and it.get("energy"):
            score += 0.15 * (1 - abs(it["energy"] - energy) / 4.0)
        rows.append((round(score, 3), it))
    rows.sort(key=lambda r: (-r[0], r[1]["id"]))
    return [
        dict(r[1], score=r[0], **({} if "grade" not in r[1] else {"grade": "…"}))
        for r in rows[:limit]
    ]


def take(item_id, dest, strength=1.0):
    """Download one OpenFilm audio item, or bake one grade to .cube, into dest."""
    os.makedirs(dest, exist_ok=True)
    if item_id.startswith("grade:") or item_id.startswith("openfilm/filters/"):
        hit = next((g for g in catalogue("grade") if g["id"] == item_id), None)
        if not hit:
            raise core.ToolError(
                "no grade %r (try: asset_packs.py search grade)" % item_id
            )
        slug = item_id.split("/")[-1].split(":")[-1]
        path = write_cube(
            hit["grade"], os.path.join(dest, "%s.cube" % slug), slug, strength
        )
        return {
            "kind": "grade",
            "id": item_id,
            "file": path,
            "source": hit["source"],
            "licence": hit["licence"],
        }
    if not item_id.startswith("openfilm/audio/"):
        raise core.ToolError(
            "take needs grade:<name>, openfilm/filters/<id> or "
            "openfilm/audio/<music|sfx>/<file>"
        )
    if not shutil.which("openfilm"):
        raise core.ToolError("the openfilm CLI is not installed: npm i -g openfilm")
    verb = "music-search" if "/music/" in item_id else "sfx-search"
    with tempfile.TemporaryDirectory() as proj:
        with open(os.path.join(proj, "film.html"), "w") as fh:
            fh.write("<!doctype html><html><body></body></html>\n")
        r = subprocess.run(
            ["openfilm", "get", verb, "--take", item_id],
            cwd=proj,
            capture_output=True,
            text=True,
            timeout=300,
        )
        got = glob.glob(os.path.join(proj, "assets", "*"))
        if r.returncode != 0 or not got:
            raise core.ToolError(
                "openfilm could not fetch %s: %s"
                % (item_id, (r.stdout + r.stderr).strip()[:300])
            )
        path = os.path.join(dest, os.path.basename(got[0]))
        shutil.move(got[0], path)
    return {
        "kind": "music" if verb == "music-search" else "sfx",
        "id": item_id,
        "file": path,
        "source": "openfilm",
        "licence": LICENCE_OPENFILM,
    }


def build_pack(style, dest, music_sec=None, take_music=True):
    if style not in STYLES:
        raise core.ToolError("style must be one of %s" % ", ".join(sorted(STYLES)))
    st = STYLES[style]
    os.makedirs(dest, exist_ok=True)
    pack = {"style": style, "items": [], "look": st["look"], "notes": []}
    pack["grade"] = take("grade:" + st["grade"], dest)
    pack["items"].append(pack["grade"])
    have_lib = openfilm_root() is not None
    if not have_lib:
        pack["notes"].append(
            "OpenFilm is not installed (npm i -g openfilm): no library music "
            "or sounds; the planner falls back to synthesized sound effects."
        )
    if have_lib and take_music:
        found = search(
            "music",
            st["music"],
            bpm=st["bpm"],
            energy=st["energy"],
            min_sec=music_sec or 30,
            limit=1,
        )
        if found:
            m = take(found[0]["id"], dest)
            m.update(
                {
                    "name": found[0]["name"],
                    "bpm": found[0].get("bpm"),
                    "energy": found[0].get("energy"),
                    "sec": found[0].get("sec"),
                }
            )
            pack["music"] = m
            pack["items"].append(m)
    pack["sfx"] = {}
    if have_lib:
        for role, prompt in st["sfx"].items():
            found = search("sfx", prompt, limit=1)
            if found:
                s = take(found[0]["id"], dest)
                s["name"] = found[0]["name"]
                pack["sfx"][role] = s
                pack["items"].append(s)
    core.write_json(pack, os.path.join(dest, "pack.json"))
    return pack


def load_pack(path):
    pack = core.load_json(path)
    base = os.path.dirname(os.path.abspath(path))
    for it in pack.get("items", []):
        if not os.path.isabs(it["file"]):
            it["file"] = os.path.join(base, it["file"])
    return pack
