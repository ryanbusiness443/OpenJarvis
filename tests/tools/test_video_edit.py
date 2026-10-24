"""Tests for the video_* editing tools and the openjarvis.tools.video engine."""

from __future__ import annotations

import copy
import json
import shutil
from fractions import Fraction

import pytest

from openjarvis.tools.video import core, remapper
from openjarvis.tools.video_edit import (
    VideoPlanTool,
    VideoProbeTool,
    VideoProposeCutsTool,
    VideoRemapTool,
    VideoRenderTool,
    VideoVerifyTool,
)

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)

NTSC = Fraction(30000, 1001)


# ---------------------------------------------------------------- time math


class TestParseTime:
    def test_decimal_is_exact(self):
        assert core.parse_time(1.2) == Fraction(6, 5)
        assert core.parse_time("1.2") == Fraction(6, 5)

    def test_clock_formats(self):
        assert core.parse_time("1:02.25") == Fraction(249, 4)
        assert core.parse_time("01:00:00.5") == Fraction(7201, 2)

    def test_frame_number(self):
        assert core.parse_time("30f", Fraction(30)) == 1

    def test_drop_frame_timecode(self):
        # 00:01:00;02 is the first label after the dropped ;00 and ;01.
        assert core.parse_time("00:01:00;02", NTSC) * NTSC == 1800
        assert core.parse_time("00:10:00;00", NTSC) * NTSC == 17982

    def test_non_drop_timecode_counts_frames(self):
        assert core.parse_time("00:00:01:00", NTSC) * NTSC == 30

    def test_rejects_negative_and_garbage(self):
        with pytest.raises(core.ToolError):
            core.parse_time("-1")
        with pytest.raises(core.ToolError):
            core.parse_time("soon")

    def test_frame_snapping_modes(self):
        t = Fraction(1, 2)  # 14.985 frames at 29.97
        assert core.frame_of(t, NTSC) == 15
        assert core.frame_of(t, NTSC, "floor") == 14
        assert core.frame_of(t, NTSC, "ceil") == 15

    def test_timestamp_formatting(self):
        assert core.fmt_ts(Fraction(1001, 30000)) == "00:00:00.033"
        assert core.fmt_chapter(Fraction(3725)) == "1:02:05"
        assert core.fmt_srt(Fraction(3, 2)) == "00:00:01,500"


# ---------------------------------------------------------------- remapping

PLAN = {
    "fps": "30",
    "expected_duration": "290",
    "segments": [
        {"src_start_frame": 0, "src_end_frame": 1800, "out_start_frame": 0},
        {"src_start_frame": 2400, "src_end_frame": 9000, "out_start_frame": 1800},
    ],
}


class TestRemapper:
    def test_point_mapping(self):
        tl = remapper.Timeline(PLAN)
        assert tl.map(Fraction(30)) == 30
        assert tl.map(Fraction(70)) is None  # inside the cut 60..80
        assert tl.map(Fraction(120)) == 100

    def test_chapters_follow_youtube_rules(self):
        chapters = [
            (Fraction(2), "Intro"),
            (Fraction(65), "Cut section"),
            (Fraction(95), "Build"),
            (Fraction(200), "Results"),
            (Fraction(205), "Five-second blip"),
        ]
        data = remapper.remap_chapters(PLAN, chapters)
        codes = {f["code"] for f in data["findings"]}
        assert data["chapters"][0]["time"] == "0:00"
        assert {"FIRST_NOT_ZERO", "MOVED", "TOO_SHORT"} <= codes
        assert data["youtube_valid"]

    def test_too_few_chapters_flagged(self):
        data = remapper.remap_chapters(PLAN, [(Fraction(0), "Only")])
        assert not data["youtube_valid"]
        assert "TOO_FEW" in {f["code"] for f in data["findings"]}

    def test_cues_crossing_a_cut_are_clipped(self):
        cues = [
            (Fraction(58), Fraction(62), "crosses the cut"),
            (Fraction(70), Fraction(72), "fully removed"),
            (Fraction(120), Fraction(123), "moves earlier"),
        ]
        out, dropped = remapper.remap_cues(PLAN, cues)
        assert dropped == 1
        assert out[0][:2] == (Fraction(58), Fraction(60))
        assert out[1][:2] == (Fraction(100), Fraction(103))


# ---------------------------------------------------------------- tool layer


class TestToolSpecsAndPolicy:
    @pytest.mark.parametrize(
        "cls,name",
        [
            (VideoProbeTool, "video_probe"),
            (VideoPlanTool, "video_plan"),
            (VideoRenderTool, "video_render"),
            (VideoVerifyTool, "video_verify"),
        ],
    )
    def test_spec_names(self, cls, name):
        tool = cls(allowed_dirs=[])
        assert tool.spec.name == name
        assert tool.tool_id == name
        assert tool.spec.category == "media"

    def test_missing_path(self):
        result = VideoProbeTool(allowed_dirs=[]).execute()
        assert not result.success
        assert "No path" in result.content

    def test_file_not_found(self, tmp_path):
        result = VideoProbeTool(allowed_dirs=[]).execute(path=str(tmp_path / "x.mp4"))
        assert not result.success
        assert "File not found" in result.content

    def test_outside_allowed_dirs(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        other = tmp_path / "other.mp4"
        other.write_bytes(b"")
        result = VideoProbeTool(allowed_dirs=[str(allowed)]).execute(path=str(other))
        assert not result.success
        assert "outside allowed directories" in result.content

    def test_sensitive_file_refused(self, tmp_path):
        secret = tmp_path / ".env"
        secret.write_text("KEY=1", encoding="utf-8")
        result = VideoProbeTool(allowed_dirs=[]).execute(path=str(secret))
        assert not result.success
        assert "sensitive" in result.content

    def test_plan_needs_input(self, tmp_path):
        result = VideoPlanTool(allowed_dirs=[]).execute(
            plan_out=str(tmp_path / "p.json")
        )
        assert not result.success
        assert "edl_path" in result.content


# ---------------------------------------------------------------- end to end


@pytest.fixture(scope="module")
def sample_clip(tmp_path_factory):
    from openjarvis.tools.video.analyzer import make_sample

    return make_sample(str(tmp_path_factory.mktemp("clip")))


@needs_ffmpeg
class TestEndToEnd:
    def test_probe(self, sample_clip):
        result = VideoProbeTool(allowed_dirs=[]).execute(path=sample_clip)
        assert result.success
        assert "exact 30000/1001" in result.content
        assert result.metadata["probe"]["video"]["frames"] == 239

    def test_propose_plan_render_verify(self, sample_clip, tmp_path):
        edl = tmp_path / "edl.json"
        plan_path = tmp_path / "plan.json"
        out = tmp_path / "out.mp4"

        proposed = VideoProposeCutsTool(allowed_dirs=[]).execute(
            path=sample_clip, edl_out=str(edl)
        )
        assert proposed.success, proposed.content
        assert proposed.metadata["summary"]["cuts"] == 2

        planned = VideoPlanTool(allowed_dirs=[]).execute(
            edl_path=str(edl), plan_out=str(plan_path), preset="draft"
        )
        assert planned.success, planned.content
        assert planned.metadata["verdict"] == "READY"

        rendered = VideoRenderTool(allowed_dirs=[]).execute(
            plan_path=str(plan_path), output_path=str(out)
        )
        assert rendered.success, rendered.content

        verified = VideoVerifyTool(allowed_dirs=[]).execute(
            plan_path=str(plan_path), output_path=str(out)
        )
        assert verified.success, verified.content
        report = verified.metadata["report"]
        statuses = {r["check"]: r["status"] for r in report["checks"]}
        for check in ("FRAME_COUNT", "CUT_FRAMES", "AUDIO_ALIGN", "LOUDNESS"):
            assert statuses[check] == "PASS", verified.content

        # Negative control: the same render against a plan shifted one frame.
        from openjarvis.tools.video import verifier

        plan = json.loads(plan_path.read_text())
        shifted = copy.deepcopy(plan)
        for seg in shifted["segments"][1:]:
            seg["src_start_frame"] += 1
            seg["src_end_frame"] += 1
        bad = verifier.verify(shifted, str(out), check_loudness=False)
        assert "CUT_FRAMES" in bad["failed"]

    def test_inline_segments_and_mid_word(self, sample_clip, tmp_path):
        words = tmp_path / "words.json"
        words.write_text(
            json.dumps(
                [
                    {"word": "hello", "start": 0.5, "end": 1.0},
                    {"word": "world", "start": 1.6, "end": 1.95},
                ]
            )
        )
        tool = VideoPlanTool(allowed_dirs=[])
        common = {
            "source": sample_clip,
            "mode": "cut",
            "segments": [{"start": "0.9", "end": "1.62"}],
            "words_path": str(words),
        }
        warned = tool.execute(
            plan_out=str(tmp_path / "a.json"), snap_words=False, **common
        )
        assert warned.metadata["verdict"] == "READY_WITH_WARNINGS"
        assert "MID_WORD" in warned.content
        fixed = tool.execute(plan_out=str(tmp_path / "b.json"), **common)
        assert fixed.metadata["verdict"] == "READY"
        assert "MID_WORD_FIXED" in fixed.content

    def test_render_refuses_source_overwrite(self, sample_clip, tmp_path):
        plan_path = tmp_path / "plan.json"
        VideoPlanTool(allowed_dirs=[]).execute(
            source=sample_clip,
            segments=[{"start": "1", "end": "2"}],
            plan_out=str(plan_path),
        )
        result = VideoRenderTool(allowed_dirs=[]).execute(
            plan_path=str(plan_path), output_path=sample_clip, force=True
        )
        assert not result.success
        assert "over the source" in result.content

    def test_remap_point(self, sample_clip, tmp_path):
        plan_path = tmp_path / "plan.json"
        VideoPlanTool(allowed_dirs=[]).execute(
            source=sample_clip,
            segments=[{"start": "2", "end": "3"}],
            plan_out=str(plan_path),
        )
        result = VideoRemapTool(allowed_dirs=[]).execute(
            plan_path=str(plan_path), kind="point", times=["1", "2.5", "4"]
        )
        assert result.success
        lines = result.content.splitlines()
        assert lines[0].endswith("00:00:01.000")
        assert lines[1].endswith("CUT")
        # The cut snaps to frames 60..90 at 29.97 fps: 1.001 s removed, not 1.000.
        assert lines[2].endswith("00:00:02.999")


# ---------------------------------------------------------------- transitions/effects


class TestTransitionMath:
    def _segs(self, *lengths):
        segs, pos = [], 0
        for i, n in enumerate(lengths):
            segs.append({"index": i, "src_start_frame": pos, "src_end_frame": pos + n})
            pos += n + 100  # gaps between pieces: real cuts
        return segs

    def test_overlap_shortens_timeline_exactly(self):
        from openjarvis.tools.video import effects

        segs = self._segs(60, 60, 60)
        segs[2]["trans_req"] = {"type": "fadeblack", "duration": "10f"}
        findings = []
        total = effects.apply_transitions(
            segs,
            {"transition": {"type": "fade", "duration": "15f"}},
            Fraction(30),
            findings,
        )
        assert total == 180 - 15 - 10
        assert [s["out_start_frame"] for s in segs] == [0, 45, 95]
        assert segs[1]["trans_out_frames"] == 10
        assert not findings

    def test_transition_longer_than_piece_is_reported(self):
        from openjarvis.tools.video import effects

        segs = self._segs(60, 8, 60)
        findings = []
        effects.apply_transitions(
            segs,
            {"transition": {"type": "fade", "duration": "6f"}},
            Fraction(30),
            findings,
        )
        assert any(f["code"] == "TRANSITION_TOO_LONG" for f in findings)

    def test_unknown_transition_and_effect_rejected(self):
        from openjarvis.tools.video import effects

        with pytest.raises(core.ToolError):
            effects.resolve_transition({"type": "sparkle"}, None, Fraction(30))
        segs = self._segs(60)
        effects.apply_transitions(segs, {}, Fraction(30), [])
        with pytest.raises(core.ToolError):
            effects.plan_effects(
                {"effects": [{"type": "lensflare"}]}, segs, 60, Fraction(30), ".", []
            )
        with pytest.raises(core.ToolError):
            effects.plan_effects(
                {
                    "effects": [
                        {
                            "type": "blur",
                            "start": 0,
                            "end": 1,
                            "rect": [0.8, 0.8, 0.5, 0.5],
                        }
                    ]
                },
                segs,
                60,
                Fraction(30),
                ".",
                [],
            )

    def test_source_timed_effect_follows_the_cut(self):
        from openjarvis.tools.video import effects

        segs = self._segs(60, 60)  # piece 1 is source frames 160..220
        effects.apply_transitions(segs, {}, Fraction(30), [])
        planned = effects.plan_effects(
            {"effects": [{"type": "zoom", "start": "170f", "end": "180f"}]},
            segs,
            120,
            Fraction(30),
            ".",
            [],
        )
        assert (planned[0]["out_start_frame"], planned[0]["out_end_frame"]) == (70, 80)


@needs_ffmpeg
class TestTransitionsAndEffectsEndToEnd:
    def test_render_verify_and_negative_control(self, sample_clip, tmp_path):
        words = tmp_path / "words.json"
        words.write_text(
            json.dumps(
                [
                    {"word": "hello", "start": 0.3, "end": 0.7},
                    {"word": "world", "start": 3.4, "end": 3.9},
                ]
            )
        )
        plan_path = tmp_path / "plan.json"
        out = tmp_path / "fx.mp4"
        planned = VideoPlanTool(allowed_dirs=[]).execute(
            source=sample_clip,
            segments=[
                {"start": "2.15", "end": "3.05"},
                {
                    "start": "5.15",
                    "end": "5.75",
                    "transition": {"type": "wipeleft", "duration": 0.3},
                },
            ],
            transition={"type": "fade", "duration": 0.4},
            effects=[
                {"type": "zoom", "start": "0.8", "end": "1.6", "scale": 1.3},
                {"type": "text", "start": "6.2", "end": "7.0", "text": "Bye"},
                {"type": "captions", "source": str(words)},
                {"type": "fade_out", "duration": 0.3},
            ],
            preset="draft",
            plan_out=str(plan_path),
        )
        assert planned.success, planned.content
        assert "2 transitions" in planned.content

        rendered = VideoRenderTool(allowed_dirs=[]).execute(
            plan_path=str(plan_path), output_path=str(out)
        )
        assert rendered.success, rendered.content
        verified = VideoVerifyTool(allowed_dirs=[]).execute(
            plan_path=str(plan_path), output_path=str(out)
        )
        assert verified.success, verified.content
        statuses = {
            r["check"]: r["status"] for r in verified.metadata["report"]["checks"]
        }
        for check in (
            "FRAME_COUNT",
            "TRANSITIONS",
            "EFFECTS",
            "CAPTIONS",
            "FADES",
            "AUDIO_ALIGN",
        ):
            assert statuses[check] == "PASS", verified.content

        # Negative control: the zoom claimed 2 frames later than rendered.
        from openjarvis.tools.video import verifier

        moved = json.loads(plan_path.read_text())
        for eff in moved["effects"]:
            if eff["type"] == "zoom":
                eff["out_start_frame"] += 2
                eff["out_end_frame"] += 2
        bad = verifier.verify(moved, str(out), check_loudness=False)
        assert "EFFECTS" in bad["failed"]
