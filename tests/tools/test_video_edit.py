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


# ---------------------------------------------------------------- reels mode

REEL_WORDS = [
    ("Stop", 0.30, 0.55),
    ("making", 0.58, 0.90),
    ("this", 0.92, 1.10),
    ("mistake.", 1.12, 1.70),
    ("Here", 3.30, 3.50),
    ("are", 3.52, 3.62),
    ("three", 3.64, 3.95),
    ("fixes", 3.97, 4.40),
    ("that", 4.42, 4.55),
    ("work.", 4.57, 4.95),
    ("Follow", 6.00, 6.35),
    ("for", 6.37, 6.50),
    ("more.", 6.52, 7.00),
]


def _reel_words(tmp_path):
    p = tmp_path / "words.json"
    p.write_text(
        json.dumps([{"word": w, "start": a, "end": b} for w, a, b in REEL_WORDS])
    )
    return p


class TestViralPlanner:
    def _plan(self, intensity):
        from openjarvis.tools.video import viral

        ws = [
            {"word": w, "start": Fraction(str(a)), "end": Fraction(str(b))}
            for w, a, b in REEL_WORDS
        ]
        return viral.plan_viral("x.mp4", ws, intensity, "short", duration=Fraction(8))

    def test_intensity_tightens_pacing(self):
        clean, _, _ = self._plan("clean")
        insane, _, _ = self._plan("insane")
        assert len(insane) >= len(clean)

    def test_hook_cta_and_loop(self):
        cuts, effects, notes = self._plan("punchy")
        kinds = [e["type"] for e in effects]
        assert "text" in kinds and "flash" in kinds and "captions" in kinds
        assert any(e.get("sound") == "ding" for e in effects)  # "Follow for more."
        assert any("loops" in n for n in notes)
        assert "fade_out" not in kinds
        assert cuts[0]["start"] == "0"  # lead-in silence removed

    def test_mid_sentence_buy_is_not_a_cta(self):
        from openjarvis.tools.video import viral

        ws = [
            {"word": w, "start": Fraction(a), "end": Fraction(b)}
            for w, a, b in [
                ("Hello", "0.1", "0.4"),
                ("there.", "0.4", "0.8"),
                ("People", "1.0", "1.3"),
                ("want", "1.3", "1.5"),
                ("to", "1.5", "1.6"),
                ("buy.", "1.6", "1.9"),
                ("Tap", "2.2", "2.4"),
                ("the", "2.4", "2.5"),
                ("link.", "2.5", "2.9"),
            ]
        ]
        _, effects, notes = viral.plan_viral(
            "x.mp4", ws, "punchy", "short", duration=Fraction(3)
        )
        cta = [n for n in notes if n.startswith("CTA")]
        assert cta and "'Tap'" in cta[0]

    def test_sfx_density_is_capped(self):
        # Distinct hits (not layered on the same instant) stay >= 0.8 s apart.
        _, effects, _ = self._plan("insane")
        times = sorted(
            {core.parse_time(e["at"]) for e in effects if e["type"] == "sfx"}
        )
        gaps = [b - a for a, b in zip(times, times[1:]) if b - a >= Fraction(1, 20)]
        assert gaps and all(g >= Fraction(4, 5) for g in gaps)


@needs_ffmpeg
class TestReelEndToEnd:
    def test_viral_plan_render_verify_audit(self, sample_clip, tmp_path):
        from openjarvis.tools.video_edit import (
            VideoRetentionAuditTool,
            VideoViralPlanTool,
        )

        words = _reel_words(tmp_path)
        edl = tmp_path / "reel.edl.json"
        planned = VideoViralPlanTool(allowed_dirs=[]).execute(
            path=sample_clip,
            words_path=str(words),
            edl_out=str(edl),
            intensity="insane",
        )
        assert planned.success, planned.content
        plan_path = tmp_path / "reel.plan.json"
        res = VideoPlanTool(allowed_dirs=[]).execute(
            edl_path=str(edl), plan_out=str(plan_path), preset="draft"
        )
        assert res.success, res.content
        out = tmp_path / "reel.mp4"
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
        assert statuses["SFX_ALIGN"] == "PASS"
        assert statuses["EFFECTS"] == "PASS"
        audit = VideoRetentionAuditTool(allowed_dirs=[]).execute(
            plan_path=str(plan_path)
        )
        assert audit.success
        assert audit.metadata["report"]["score"] >= 75


# ---------------------------------------------------------------- beats, packs, montage


def _click_track(path, bpm=120.0, secs=10.0):
    """Kick on every beat (accented on 1), hats on the offbeat, a pink-noise bed."""
    p = 60.0 / bpm
    kick = (
        f"(lt(mod(t,{p}),0.06)*sin(2*PI*55*t)*exp(-40*mod(t,{p}))"
        f"*(1+0.8*lt(mod(t,{4 * p}),0.06)))"
    )
    hat = f"(lt(mod(t+{p / 2},{p}),0.02)*0.25*sin(2*PI*7000*t))"
    core.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            f"aevalsrc='0.6*{kick}+{hat}':s=44100:d={secs}",
            "-f", "lavfi", "-i", f"anoisesrc=a=0.01:c=pink:d={secs}:r=44100:s=1",
            "-filter_complex", "amix=inputs=2:normalize=0", "-ac", "1", str(path),
        ]
    )  # fmt: skip
    return [i * p for i in range(int(secs / p))]


@needs_ffmpeg
class TestBeats:
    def test_click_track_tempo_beats_downbeats(self, tmp_path):
        from openjarvis.tools.video import beats

        wav = tmp_path / "click.wav"
        truth = _click_track(wav)
        rep = beats.detect(str(wav))
        assert abs(rep["tempo_bpm"] - 120.0) < 1.0
        assert rep["confidence"] >= 0.3
        errs = [min(abs(b - t) for t in truth) for b in rep["beats"][1:-1]]
        assert max(errs) < 0.010
        assert all(
            min(abs(d - 2.0 * k) for k in range(6)) < 0.02 for d in rep["downbeats"]
        )

    def test_detect_beats_tool(self, tmp_path):
        from openjarvis.tools.video_edit import VideoDetectBeatsTool

        wav = tmp_path / "click.wav"
        _click_track(wav)
        out = tmp_path / "beats.json"
        res = VideoDetectBeatsTool(allowed_dirs=[]).execute(
            path=str(wav), out_path=str(out)
        )
        assert res.success, res.content
        assert "BPM" in res.content and out.exists()


class TestPacksAndSnapping:
    def test_identity_grade_is_identity_lut(self, tmp_path):
        from openjarvis.tools.video import packs

        cube = packs.write_cube({}, str(tmp_path / "id.cube"), "id", size=5)
        rows = [list(map(float, ln.split())) for ln in open(cube) if ln[:1].isdigit()]
        for i, row in enumerate(rows):
            want = ((i % 5) / 4.0, (i // 5 % 5) / 4.0, (i // 25) / 4.0)
            assert max(abs(a - b) for a, b in zip(row, want)) < 1e-9

    def test_grade_order_matrix_then_saturation_then_curves(self):
        from openjarvis.tools.video import packs

        grey = packs.apply_grade((0.5, 0.5, 0.5), {"sat": 0.0})
        assert max(grey) - min(grey) < 1e-9  # saturation 0 keeps grey grey
        lifted = packs.apply_grade((0.0, 0.0, 0.0), {"all": [0.1, 1.0]})
        assert all(abs(v - 0.1) < 1e-9 for v in lifted)

    def test_builtin_grades_always_searchable(self):
        from openjarvis.tools.video import packs

        ids = [r["id"] for r in packs.search("grade", "warm", limit=20)]
        assert "grade:warm-vlog" in ids

    def test_jump_cut_slides_onto_beat_inside_its_pause(self):
        from openjarvis.tools.video import viral

        cuts = [
            {
                "start": "1.100",
                "end": "1.600",
                "_min_start": "1.030",
                "label": "jump cut: 0.6s pause after 'x'",
            }
        ]
        landed = viral.snap_cuts(cuts, [Fraction(1), Fraction(3, 2)], [])
        assert cuts[0]["start"] == viral._ts(Fraction(3, 2)) or landed == []
        # the beat at 1.5 s leaves under 0.08 s of pause: not taken; 1.0 s is
        # before the word ends: not taken either. Nothing may clip a word.
        assert landed == []
        cuts[0]["start"], cuts[0]["_min_start"] = "1.100", "1.030"
        landed = viral.snap_cuts(cuts, [Fraction(6, 5)], [])
        assert landed == [Fraction(6, 5)]
        assert core.parse_time(cuts[0]["start"]) == Fraction(6, 5)

    @pytest.mark.parametrize(
        "name",
        ["video_detect_beats", "video_montage_plan", "video_asset_packs"],
    )
    def test_new_tool_specs(self, name):
        from openjarvis.tools import video_edit

        cls = {
            "video_detect_beats": video_edit.VideoDetectBeatsTool,
            "video_montage_plan": video_edit.VideoMontagePlanTool,
            "video_asset_packs": video_edit.VideoAssetPacksTool,
        }[name]
        tool = cls(allowed_dirs=[])
        assert tool.spec.name == name == tool.tool_id
        assert tool.spec.category == "media"


@needs_ffmpeg
class TestMontageEndToEnd:
    def test_cuts_on_the_beat_and_negative_controls(self, tmp_path):
        from openjarvis.tools.video_edit import VideoMontagePlanTool

        clips = []
        for i in range(4):
            p = tmp_path / f"clip{i}.mp4"
            core.run(
                [
                    "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=320x180:r=30:d=4,hue=h={i * 90}",
                    "-f", "lavfi", "-i", f"sine=f={300 + 100 * i}:d=4", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p),
                ]
            )  # fmt: skip
            clips.append(str(p))
        music = tmp_path / "click.wav"
        _click_track(music, secs=8.0)
        edl = tmp_path / "m.edl.json"
        planned = VideoMontagePlanTool(allowed_dirs=[]).execute(
            clip_paths=clips,
            music_path=str(music),
            bound_out=str(tmp_path / "bound.mp4"),
            edl_out=str(edl),
            style="motivation",
            format="long",
        )
        assert planned.success, planned.content
        plan_path = tmp_path / "m.plan.json"
        res = VideoPlanTool(allowed_dirs=[]).execute(
            edl_path=str(edl), plan_out=str(plan_path), preset="draft"
        )
        assert res.success, res.content
        out = tmp_path / "m.mp4"
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
        assert statuses["BEAT_SYNC"] == "PASS"
        assert statuses["CUT_FRAMES"] == "PASS"
        assert statuses["FRAME_COUNT"] == "PASS"

        plan = json.loads(plan_path.read_text())
        for field, change in (("beats", 2 / 30.0), ("music_start", 2 / 30.0)):
            bad = copy.deepcopy(plan)
            if field == "beats":
                bad["beat_sync"]["beats"] = [
                    b + change for b in bad["beat_sync"]["beats"]
                ]
            else:
                bad["beat_sync"]["music_start"] = change
            bad_path = tmp_path / f"bad_{field}.json"
            bad_path.write_text(json.dumps(bad))
            res = VideoVerifyTool(allowed_dirs=[]).execute(
                plan_path=str(bad_path), output_path=str(out)
            )
            rows = {r["check"]: r["status"] for r in res.metadata["report"]["checks"]}
            assert rows["BEAT_SYNC"] == "FAIL", field
