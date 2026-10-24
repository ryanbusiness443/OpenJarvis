# YouTube Editor

An agent that edits raw recordings for YouTube with frame-level precision and checks its own
work. The model makes the editorial calls (what drags, which take is clean, which pause is a
deliberate beat). Seven `video_*` tools do the cutting to the exact frame and audio sample, and
an edit only counts as finished when `video_verify` returns PASS.

## Quickstart

```bash
# 1. ffmpeg >= 5.1 must be on PATH
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian / Ubuntu

# 2. Install the preset and a local model
jarvis init --preset youtube-editor --force
ollama pull qwen3.5:9b

# 3. Edit
jarvis chat
> Edit ~/Videos/raw.mp4 for YouTube. Remove dead air, keep it natural, write final.mp4.
```

The agent probes the file, proposes cuts, and **waits for your approval** before it renders.
Then it verifies the render and reports the result.

## The loop

| Step | Tool | What it guarantees |
|---|---|---|
| Inspect | `video_probe` | Exact rational frame rate (29.97 = 30000/1001), exact frame count, warnings for variable frame rate, interlacing, audio/video start offsets |
| Measure | `video_analyze` | Silences, black segments, scene changes, EBU R128 loudness |
| Propose | `video_propose_cuts` | Padded cuts that never clip speech; filler words (`um`, `uh`) when word timestamps are given. Writes an EDL for review |
| Plan | `video_plan` | Every cut snapped to a real frame; `MID_WORD` if a cut lands inside a word; flash-frame and micro-cut warnings; audio boundaries derived from the output frame timeline so drift cannot accumulate |
| Render | `video_render` | One decode pass selecting by frame index and sample index. Never stream copy, which can only cut on keyframes. Short fades at each cut, loudness to −14 LUFS / −1 dBTP, H.264 High / closed GOP / AAC 48 kHz / faststart |
| Verify | `video_verify` | Exact frame count and rate, A/V sync, per-piece audio cross-correlation within 1 ms, frame matching at every cut against the source, loudness, upload spec |
| Carry over | `video_remap` | Chapters remapped and checked against YouTube's rules (0:00 first, at least 3, each 10 s or more); captions clipped or rebuilt from kept words |

Presets: `youtube` (native size), `youtube-720p`, `youtube-1080p`, `youtube-1440p`, `youtube-4k`,
`youtube-shorts` (9:16 centre crop, 3-minute limit), `draft` (fast preview).

## Word timestamps

Word-level timestamps enable word-safe cuts, filler removal and captions without the removed
words. Any whisper-style JSON works (`faster-whisper`, WhisperX, or a list of
`{"word", "start", "end"}`). Pass it as `words_path`.

## Precision, measured

On synthetic test clips with a frame counter and known audio events:

- 61-piece 1080p edit: 4080/4080 frames, 100/100 cut boundaries at offset 0, worst audio lag 0.0 ms.
- Beeps at known times through three cuts and a 44.1 → 48 kHz resample: within one sample.
- Source whose audio starts 278 ms after the video: worst audio error 0.06 ms.
- A render with audio 10 ms late, or a plan shifted by one frame, is caught as FAIL.

## Safety

- Every path is checked against `[security] allowed_dirs` and the sensitive-file policy.
- The source file is never modified; outputs are never overwritten without `force`.
- Nothing is uploaded and no API keys are needed.

The same engine ships as a Claude Code skill in
[claude-skills `marketing/youtube-editor`](https://github.com/alirezarezvani/claude-skills).
