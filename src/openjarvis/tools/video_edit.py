"""Video editing tools — frame-accurate cutting for YouTube, with verification.

Seven tools drive the ``openjarvis.tools.video`` engine (ffmpeg via
subprocess, exact ``Fraction`` time arithmetic):

- ``video_probe``         stream facts and precision warnings (VFR, interlace,
                          A/V offset, keyframe spacing)
- ``video_analyze``       silences, black segments, scene changes, loudness
- ``video_propose_cuts``  padded, word-safe cut proposals written as an EDL
- ``video_plan``          EDL (file or inline segments, transitions, effects)
                          -> frame/sample-exact plan
- ``video_render``        plan -> MP4 in one decode pass (never stream copy)
- ``video_verify``        plan + render -> PASS/FAIL with per-cut frame matching
- ``video_remap``         chapters / captions / timestamps through the edit

Every path is checked against ``[security] allowed_dirs`` and the
sensitive-file policy. Tools never modify the source file and refuse to
overwrite an existing output unless ``force`` is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._paths import resolve_allowed_dirs
from openjarvis.tools._stubs import BaseTool, ToolSpec

_MAX_LISTED = 40

_TRANSITION_DOC = (
    "Default transition between pieces, e.g. {type: fade, duration: 0.4}. Types: "
    "cut (default), fade, dissolve, fadeblack, fadewhite, wipeleft, slideup, "
    "circleopen, zoomin and other ffmpeg xfade styles. A segment may carry its own "
    "transition to override it."
)
_EFFECTS_DOC = (
    "Effects. Source-timed: {type: zoom, start, end, scale, center:[x,y]}, "
    "{type: text, start, end, text, position: top|center|bottom}, "
    "{type: blur, start, end, rect:[x,y,w,h]}. Whole video: {type: color, "
    "brightness, contrast, saturation}, {type: lut, file}, {type: captions, "
    "source, style: youtube|shorts}, {type: music, file, volume_db, duck}, "
    "{type: fade_in, duration}, {type: fade_out, duration}. Only add effects the "
    "user asked for."
)


class _VideoTool(BaseTool):
    """Shared path policy and error handling for the video_* tools."""

    tool_id = ""

    def __init__(self, allowed_dirs: Optional[List[str]] = None) -> None:
        self._allowed_dirs = [
            Path(d).expanduser().resolve() for d in resolve_allowed_dirs(allowed_dirs)
        ]

    def _fail(self, message: str) -> ToolResult:
        return ToolResult(tool_name=self.tool_id, content=message, success=False)

    def _ok(self, content: str, **metadata: Any) -> ToolResult:
        return ToolResult(tool_name=self.tool_id, content=content, metadata=metadata)

    def _path(self, value: Any, label: str, must_exist: bool = True) -> Path:
        """Resolve and policy-check a path; raises ValueError with a user message."""
        if not value or not isinstance(value, str):
            raise ValueError(f"No {label} provided.")
        path = Path(value).expanduser()
        if not path.is_absolute() and self._allowed_dirs:
            path = self._allowed_dirs[0] / path
        from openjarvis.security.file_policy import is_sensitive_file

        if is_sensitive_file(path):
            raise ValueError(f"Access denied: {value} is a sensitive file.")
        if self._allowed_dirs:
            resolved = path.resolve()
            if not any(
                resolved == d or resolved.is_relative_to(d) for d in self._allowed_dirs
            ):
                raise ValueError(
                    f"Access denied: {value} is outside allowed directories."
                )
        if must_exist and not path.exists():
            raise ValueError(f"File not found: {value}")
        return path

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import core

        try:
            return self._run(**params)
        except ValueError as exc:
            return self._fail(str(exc))
        except core.ToolError as exc:
            return self._fail(str(exc))

    def _run(self, **params: Any) -> ToolResult:  # pragma: no cover - abstract
        raise NotImplementedError


def _findings_text(findings: List[Dict[str, Any]]) -> str:
    lines = []
    for f in findings:
        fix = f"  -> {f['fix']}" if f.get("fix") else ""
        lines.append(f"[{f['severity'].upper()}] {f['code']}: {f['message']}{fix}")
    return "\n".join(lines)


@ToolRegistry.register("video_probe")
class VideoProbeTool(_VideoTool):
    """Report exact stream facts and precision warnings for a media file."""

    tool_id = "video_probe"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_probe",
            description=(
                "Inspect a video file: exact frame rate (as a fraction), resolution, "
                "duration, frame count, audio format, keyframe spacing, and warnings "
                "(variable frame rate, interlacing, audio/video start offset). "
                "Run this first on any source video."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the video."},
                },
                "required": ["path"],
            },
            category="media",
            timeout_seconds=120.0,
            required_capabilities=["file:read"],
        )

    def _run(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import core

        path = self._path(params.get("path"), "path")
        info = core.probe(str(path))
        lines = [f"file: {info['path']} ({info['container']})"]
        v, a = info.get("video"), info.get("audio")
        if v:
            frames = core.count_frames(str(path))
            kfs = core.keyframes(str(path))
            gaps = [b - c for c, b in zip(kfs, kfs[1:])]
            max_gap = core.fsec(max(gaps), 3) if gaps else None
            info["video"]["frames"] = frames
            info["keyframe_max_gap_s"] = max_gap
            lines.append(
                f"video: {v['codec']} {v['width']}x{v['height']} {v['pix_fmt']} "
                f"fps={v['fps_display']} (exact {v['fps']}) frames={frames} "
                f"duration={core.fsec(core.to_fraction(v['duration']), 3)}s"
            )
            lines.append(
                f"keyframes: {len(kfs)}, largest gap {max_gap}s "
                "(a stream-copy cut could land that far off; renders here are exact)"
            )
        if a:
            lines.append(
                f"audio: {a['codec']} {a['sample_rate']} Hz {a['channels']} ch"
            )
        lines += [f"WARNING {w}" for w in info["warnings"]]
        return self._ok("\n".join(lines), probe=info)


@ToolRegistry.register("video_analyze")
class VideoAnalyzeTool(_VideoTool):
    """Find silences, black segments, scene changes and loudness."""

    tool_id = "video_analyze"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_analyze",
            description=(
                "Analyze a video in one pass: silent stretches, black segments, scene "
                "changes, and EBU R128 loudness (integrated LUFS, true peak, LRA)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the video."},
                    "noise_db": {
                        "type": "number",
                        "description": "Silence threshold in dBFS (default -35).",
                    },
                    "min_silence": {
                        "type": "number",
                        "description": "Shortest silence to report, seconds (0.5).",
                    },
                },
                "required": ["path"],
            },
            category="media",
            timeout_seconds=1800.0,
            required_capabilities=["file:read"],
        )

    def _run(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import analyzer

        path = self._path(params.get("path"), "path")
        data = analyzer.analyze(
            str(path),
            noise_db=float(params.get("noise_db", -35.0)),
            min_silence=float(params.get("min_silence", 0.5)),
        )
        lines = [
            f"silences: {len(data['silences'])} · black segments: "
            f"{len(data['black_segments'])} · scene changes: "
            f"{len(data['scene_changes'])}",
            f"loudness: {data['loudness']}",
        ]
        for s in data["silences"][:_MAX_LISTED]:
            lines.append(f"  silence {s['start']} -> {s['end']} ({s['duration']}s)")
        if len(data["silences"]) > _MAX_LISTED:
            lines.append(f"  ... {len(data['silences']) - _MAX_LISTED} more")
        return self._ok("\n".join(lines), analysis=data)


@ToolRegistry.register("video_propose_cuts")
class VideoProposeCutsTool(_VideoTool):
    """Propose a padded, word-safe cut list and write it as an EDL."""

    tool_id = "video_propose_cuts"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_propose_cuts",
            description=(
                "Propose cuts that remove dead air (and filler words like um/uh when "
                "word timestamps are given), keeping padding so speech is never "
                "clipped. Writes an EDL JSON file. These are PROPOSALS: show them to "
                "the user and get approval before planning."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Source video."},
                    "edl_out": {
                        "type": "string",
                        "description": "Where to write the EDL JSON.",
                    },
                    "words_path": {
                        "type": "string",
                        "description": "Optional word-timestamp JSON (whisper format).",
                    },
                    "pad": {
                        "type": "number",
                        "description": "Silence kept each side of a cut, s (0.15).",
                    },
                    "min_silence": {
                        "type": "number",
                        "description": "Shortest silence to cut, seconds (0.6).",
                    },
                    "noise_db": {
                        "type": "number",
                        "description": "Silence threshold in dBFS (default -35).",
                    },
                    "fillers": {
                        "type": "boolean",
                        "description": "Propose filler-word cuts (default true).",
                    },
                },
                "required": ["path", "edl_out"],
            },
            category="media",
            timeout_seconds=1800.0,
            required_capabilities=["file:read", "file:write"],
        )

    def _run(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import analyzer, core

        path = self._path(params.get("path"), "path")
        edl_out = self._path(params.get("edl_out"), "edl_out", must_exist=False)
        words = None
        if params.get("words_path"):
            words = core.load_words(str(self._path(params["words_path"], "words_path")))
        edl, _ = analyzer.propose(
            str(path),
            words=words,
            noise_db=float(params.get("noise_db", -35.0)),
            min_silence=float(params.get("min_silence", 0.6)),
            pad=float(params.get("pad", 0.15)),
            fillers=bool(params.get("fillers", True)),
        )
        if params.get("words_path"):
            edl["words"] = str(self._path(params["words_path"], "words_path"))
        core.write_json(edl, str(edl_out))
        summary = edl["summary"]
        lines = [
            f"wrote {edl_out}: {summary['cuts']} proposed cuts removing "
            f"{summary['removed_s']}s of {summary['source_s']}s "
            f"-> {summary['result_s']}s",
        ]
        for c in edl["segments"][:_MAX_LISTED]:
            lines.append(
                f"  CUT {c['start']} -> {c['end']} [{c['kind']}] {c['reason']}"
            )
        if len(edl["segments"]) > _MAX_LISTED:
            lines.append(f"  ... {len(edl['segments']) - _MAX_LISTED} more")
        lines.append(edl["review"])
        return self._ok("\n".join(lines), edl_path=str(edl_out), summary=summary)


@ToolRegistry.register("video_plan")
class VideoPlanTool(_VideoTool):
    """Compile an EDL into a frame-exact, sample-exact render plan."""

    tool_id = "video_plan"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_plan",
            description=(
                "Turn an edit decision list into a frame-exact plan. Pass edl_path, OR "
                "pass source + mode + segments inline. Snaps every cut to a real "
                "frame, flags cuts inside spoken words, flash frames and jumps, and "
                "writes plan_out. Verdict READY / READY_WITH_WARNINGS / BLOCKED; never "
                "render a BLOCKED plan."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan_out": {
                        "type": "string",
                        "description": "Where to write the plan JSON.",
                    },
                    "edl_path": {"type": "string", "description": "EDL JSON file."},
                    "source": {
                        "type": "string",
                        "description": "Source video (inline EDL).",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["cut", "keep"],
                        "description": "Segments are removed (cut) or kept (keep).",
                    },
                    "segments": {
                        "type": "array",
                        "description": (
                            "Inline ranges: [{start, end, label}]. Times as seconds, "
                            "HH:MM:SS.mmm, or SMPTE HH:MM:SS:FF."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "string"},
                                "end": {"type": "string"},
                                "label": {"type": "string"},
                            },
                            "required": ["start", "end"],
                        },
                    },
                    "words_path": {
                        "type": "string",
                        "description": "Word-timestamp JSON; enables MID_WORD checks.",
                    },
                    "transition": {"type": "object", "description": _TRANSITION_DOC},
                    "effects": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": _EFFECTS_DOC,
                    },
                    "snap_words": {
                        "type": "boolean",
                        "description": "Move cuts out of words (default true).",
                    },
                    "preset": {
                        "type": "string",
                        "enum": [
                            "youtube",
                            "youtube-720p",
                            "youtube-1080p",
                            "youtube-1440p",
                            "youtube-4k",
                            "youtube-shorts",
                            "draft",
                        ],
                        "description": "Output preset (default youtube = native size).",
                    },
                },
                "required": ["plan_out"],
            },
            category="media",
            timeout_seconds=300.0,
            required_capabilities=["file:read", "file:write"],
        )

    def _run(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import core, planner

        plan_out = self._path(params.get("plan_out"), "plan_out", must_exist=False)
        if params.get("edl_path"):
            edl_path = self._path(params["edl_path"], "edl_path")
            edl = core.load_json(str(edl_path))
            edl_dir = str(edl_path.parent)
        elif params.get("source") and params.get("segments"):
            edl = {
                "source": params["source"],
                "mode": params.get("mode", "cut"),
                "segments": params["segments"],
            }
            edl_dir = os.getcwd()
        else:
            return self._fail("Provide edl_path, or source + segments.")
        if params.get("transition") is not None:
            edl["transition"] = params["transition"]
        if params.get("effects") is not None:
            edl["effects"] = params["effects"]
        for eff in edl.get("effects") or []:
            for key in ("file", "source", "fontfile"):
                if eff.get(key):
                    f = eff[key]
                    f = f if os.path.isabs(f) else os.path.join(edl_dir, f)
                    eff[key] = str(self._path(f, f"effect {key}"))
        src = edl.get("source", "")
        if not os.path.isabs(src):
            src = os.path.normpath(os.path.join(edl_dir, src))
        edl["source"] = str(self._path(src, "source"))
        words_path = None
        if params.get("words_path"):
            words_path = str(self._path(params["words_path"], "words_path"))
        elif edl.get("words"):
            w = edl["words"]
            w = w if os.path.isabs(w) else os.path.join(edl_dir, w)
            edl["words"] = str(self._path(w, "words"))
        plan = planner.build_plan(
            edl,
            edl_dir,
            words_path,
            snap_words=bool(params.get("snap_words", True)),
            preset_override=params.get("preset"),
        )
        core.write_json(plan, str(plan_out))
        lines = [
            f"verdict: {plan['verdict']} (plan written to {plan_out})",
            f"{len(plan['segments'])} pieces · {plan['total_frames']} frames = "
            f"{plan['expected_duration_ts']} · removed {plan['removed_s']}s · "
            f"fps {plan['fps']}",
        ]
        trans = [s for s in plan["segments"] if s.get("trans_in_frames")]
        if trans or plan["effects"]:
            lines.append(
                f"{len(trans)} transitions · effects: "
                + (", ".join(e["type"] for e in plan["effects"]) or "none")
            )
        if plan["findings"]:
            lines.append(_findings_text(plan["findings"]))
        return self._ok(
            "\n".join(lines),
            plan_path=str(plan_out),
            verdict=plan["verdict"],
            total_frames=plan["total_frames"],
        )


@ToolRegistry.register("video_render")
class VideoRenderTool(_VideoTool):
    """Render a plan to an upload-ready MP4, frame- and sample-exact."""

    tool_id = "video_render"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_render",
            description=(
                "Render a plan to MP4: frame-exact cuts (never stream copy), "
                "transitions and effects from the plan, click-free audio, loudness "
                "normalized to -14 LUFS / -1 dBTP, "
                "YouTube's recommended H.264/AAC settings. Never writes over the "
                "source. Always run video_verify afterwards."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan_path": {"type": "string", "description": "Plan JSON."},
                    "output_path": {
                        "type": "string",
                        "description": "Output .mp4 path (must differ from source).",
                    },
                    "preset": {
                        "type": "string",
                        "description": "Override the plan's preset (e.g. draft).",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite an existing output file.",
                    },
                    "loudnorm": {
                        "type": "boolean",
                        "description": "Normalize loudness (default true).",
                    },
                },
                "required": ["plan_path", "output_path"],
            },
            category="media",
            timeout_seconds=7200.0,
            required_capabilities=["file:read", "file:write"],
        )

    def _run(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import core, renderer

        plan_path = self._path(params.get("plan_path"), "plan_path")
        out = self._path(params.get("output_path"), "output_path", must_exist=False)
        plan = core.load_json(str(plan_path))
        self._path(plan.get("source"), "plan source")
        report = renderer.render(
            plan,
            str(out),
            preset=params.get("preset"),
            force=bool(params.get("force", False)),
            loudness=bool(params.get("loudnorm", True)),
        )
        lines = [
            f"rendered {report['output']} ({report['preset']}) in "
            f"{report['seconds_total']}s",
            f"expected {report['expected_frames']} frames, "
            f"{report['expected_duration']}",
        ]
        if report.get("loudness_measured"):
            m = report["loudness_measured"]
            lines.append(
                f"loudness before: {m['input_i']} LUFS, TP {m['input_tp']} dBTP "
                f"-> {report.get('loudness_mode')}"
            )
        lines.append("Next: video_verify with the same plan and this output.")
        return self._ok("\n".join(lines), report=report)


@ToolRegistry.register("video_verify")
class VideoVerifyTool(_VideoTool):
    """Prove a render matches its plan (PASS/FAIL)."""

    tool_id = "video_verify"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_verify",
            description=(
                "Verify a render against its plan: exact frame count and rate, A/V "
                "sync, audio cross-correlation per piece (1 ms), the frame after every "
                "cut matched against the source, transitions rebuilt and compared, "
                "effects on/off at their edge frames, burned captions, loudness, and "
                "YouTube upload spec. "
                "An edit is not done until this returns PASS."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan_path": {"type": "string", "description": "Plan JSON."},
                    "output_path": {"type": "string", "description": "Rendered file."},
                    "max_cuts": {
                        "type": "integer",
                        "description": "Frame-match at most this many pieces (50).",
                    },
                },
                "required": ["plan_path", "output_path"],
            },
            category="media",
            timeout_seconds=3600.0,
            required_capabilities=["file:read"],
        )

    def _run(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import core, verifier

        plan_path = self._path(params.get("plan_path"), "plan_path")
        out = self._path(params.get("output_path"), "output_path")
        plan = core.load_json(str(plan_path))
        self._path(plan.get("source"), "plan source")
        rep = verifier.verify(plan, str(out), int(params.get("max_cuts", 50)))
        lines = [f"verdict: {rep['verdict']}"]
        for r in rep["checks"]:
            lines.append(f"  {r['check']:<11} {r['status']:<5} {r['detail']}")
        for c in [c for c in rep["cuts"] if c["status"] != "PASS"][:15]:
            lines.append(
                f"    {c['status']} {c['where']}: out frame {c['out_frame']} vs src "
                f"frame {c['src_frame']} -> offset {c.get('offset')} "
                f"(mad {c.get('mad')})"
            )
        return ToolResult(
            tool_name=self.tool_id,
            content="\n".join(lines),
            success=rep["verdict"] == "PASS",
            metadata={"report": rep},
        )


@ToolRegistry.register("video_remap")
class VideoRemapTool(_VideoTool):
    """Carry chapters, captions or timestamps through an edit."""

    tool_id = "video_remap"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="video_remap",
            description=(
                "Map source-time artefacts onto the edited timeline. kind=chapters "
                "remaps a chapter list and enforces YouTube rules (0:00 first, >= 3 "
                "chapters, >= 10 s each); kind=captions remaps an .srt/.vtt, or "
                "rebuilds captions from words_path; kind=point maps timestamps."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan_path": {"type": "string", "description": "Plan JSON."},
                    "kind": {
                        "type": "string",
                        "enum": ["chapters", "captions", "point"],
                    },
                    "input_path": {
                        "type": "string",
                        "description": "Chapters .txt/.json or captions .srt/.vtt.",
                    },
                    "words_path": {
                        "type": "string",
                        "description": "Rebuild captions from word timestamps.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Write the .srt or chapter block here.",
                    },
                    "times": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Source timestamps for kind=point.",
                    },
                },
                "required": ["plan_path", "kind"],
            },
            category="media",
            timeout_seconds=60.0,
            required_capabilities=["file:read", "file:write"],
        )

    def _run(self, **params: Any) -> ToolResult:
        from openjarvis.tools.video import core, remapper

        plan = core.load_json(str(self._path(params.get("plan_path"), "plan_path")))
        kind = params.get("kind")
        out = None
        if params.get("output_path"):
            out = self._path(params["output_path"], "output_path", must_exist=False)
        if kind == "point":
            tl = remapper.Timeline(plan)
            rows = []
            for t in params.get("times") or []:
                o = tl.map(core.parse_time(t, tl.fps))
                mapped = core.fmt_ts(o) if o is not None else "CUT"
                rows.append(f"{t} -> {mapped}")
            return self._ok("\n".join(rows) or "No times given.")
        if kind == "chapters":
            src = self._path(params.get("input_path"), "input_path")
            data = remapper.remap_chapters(plan, remapper.load_chapters(str(src)))
            if out:
                out.write_text(data["description_block"] + "\n", encoding="utf-8")
            notes = "".join(
                f"\n  [{f['code']}] {f['message']}" for f in data["findings"]
            )
            valid = "valid" if data["youtube_valid"] else "NOT valid"
            return self._ok(
                f"{data['description_block']}\n(YouTube chapter rules: {valid}){notes}",
                chapters=data,
            )
        if kind == "captions":
            if params.get("words_path"):
                words = core.load_words(
                    str(self._path(params["words_path"], "words_path"))
                )
                cues = remapper.cues_from_words(plan, words)
                dropped = None
            elif params.get("input_path"):
                src = self._path(params["input_path"], "input_path")
                cues, dropped = remapper.remap_cues(plan, remapper.load_cues(str(src)))
            else:
                return self._fail("captions needs input_path or words_path.")
            srt = remapper.write_srt(cues, str(out) if out else None)
            head = f"{len(cues)} cues" + (
                f", {dropped} dropped (fully cut)" if dropped else ""
            )
            body = f"wrote {out}" if out else srt[:4000]
            return self._ok(f"{head}\n{body}", cues=len(cues))
        return self._fail(f"Unknown kind: {kind!r}")


__all__ = [
    "VideoAnalyzeTool",
    "VideoPlanTool",
    "VideoProbeTool",
    "VideoProposeCutsTool",
    "VideoRemapTool",
    "VideoRenderTool",
    "VideoVerifyTool",
]
