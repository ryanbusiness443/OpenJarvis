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

## Transitions and effects

Ask for them in plain language ("crossfade between sections, zoom in at 2:10, burn captions
for the Short"). The agent passes them to `video_plan`:

| Kind | Options |
|---|---|
| Transitions | `cut` (default), `fade`, `dissolve`, `fadeblack`, `fadewhite`, wipes, slides, `circleopen`, `zoomin` and the rest of ffmpeg's 50+ `xfade` styles; a default plus per-cut overrides |
| Source-timed effects | `zoom` punch-in, `text` overlay, `blur` region. They move with the content when cuts change |
| Whole-video effects | `color`, `lut` (.cube), burned-in `captions` (`youtube` / `shorts` style), `music` bed ducked under the voice, `fade_in`, `fade_out` |

A transition overlaps two pieces, so the edit gets shorter by exactly its length, and the audio
crossfade covers exactly the same samples. `video_verify` then proves the styling:

- **Transitions:** the middle frame of each transition is rebuilt from the source with the same `xfade` and compared.
- **Effects:** each effect must be on at its first and last frame and off just outside them.
- **Captions:** burned captions are re-rendered onto source frames at the matching time and compared.
- **Fades:** the first and last frames are checked for black.

## Reels, Shorts and TikTok

Ask for a reel ("make this a punchy reel with my music") and the agent runs
`video_viral_plan`, then plans, renders and verifies it, then scores it with
`video_retention_audit`.

| Step | What it does |
|---|---|
| Hook | First word on frame 1, a title card from the first sentence, a flash and an impact sound |
| Pace | Jump cuts on every pause above the intensity's threshold (0.40 / 0.22 / 0.14 s) |
| Rhythm | Framing alternates wide/tight so the frame never sits still longer than 4 / 2.5 / 1.6 s |
| Emphasis | Punch-ins and pops on numbers and power words; black-and-white hits on negative words (insane) |
| CTA | The sentence opening with "tap", "follow", "comment"… gets the tightest framing and a ding |
| Finish | Word-by-word karaoke captions, a progress bar, a ducked music bed, a hard loop ending |

The sound effects are synthesized in code: whoosh, boom, bass drop, pop, click, ding, riser
and camera. Each is levelled against the normalised voice, and `video_verify` locates it in the
output by cross-correlation (`SFX_ALIGN`). The audit scores hook speed, hook visual and text, the
longest still stretch, change rate, captions, length, sound density, CTA and loop. It is honest
about its limit: pacing keeps viewers watching, but the idea and the first sentence decide reach.

## Genres, beat sync and montages

`video_viral_plan` takes a `style`, and each one changes what the edit is for:

| Style | Pace | Look | Signature moves |
|---|---|---|---|
| `talking-head` | punchy | punch grade | hook card, pops on key words, CTA framing |
| `motivation` | punchy | teal-orange, grain, vignette, bars on landscape | strongest line as a centre quote with an impact |
| `storytelling` | clean pauses | moody, grain, deep vignette | a riser that ends on each story turn ("but", "then", "until"), slow push-in |
| `day-in-life` | punchy | warm, light grain | shutter clicks on time words |

With music, set `beat_sync`. Jump cuts then slide inside their pauses onto the beat, and hits
move onto the nearest beat within 120 ms; words are never clipped. `video_detect_beats` finds
the tempo, beats, downbeats and rises, using ffmpeg and the standard library only.

For clips with no talking (a vlog day, a hype montage, a recap), `video_montage_plan` joins the
clips and cuts every shot on a beat. Shots run faster in the song's rises and longer in its drops,
and day-in-life clips get time cards. `video_verify` adds a `BEAT_SYNC` check: every planned cut
must sit within half a frame of its beat, and the music must be where the plan put it (found by
onset cross-correlation). Shifting either by two frames fails it.

`video_asset_packs` finds music, sounds and colour grades with a stated licence. It covers
built-in grades plus [OpenFilm](https://github.com/openfilm/openfilm)'s CC0 library, which needs
`npm i -g openfilm`: about 1,200 tracks with tempo and energy, 5,400 sounds and 66 grades. A grade
is baked into a `.cube` LUT, so it renders and verifies like any other effect; `action=pack`
builds a genre's grade, music and sounds in one call.

**Large files.** Bring footage in from Google Drive or a direct link rather than a chat upload,
and never re-encode it to make it fit.

## Word timestamps

Word-level timestamps enable word-safe cuts, filler removal and captions without the removed
words. Any whisper-style JSON works (`faster-whisper`, WhisperX, or a list of
`{"word", "start", "end"}`). Pass it as `words_path`.

## Precision, measured

On synthetic test clips with a frame counter and known audio events:

- 61-piece 1080p edit: 4080/4080 frames, 100/100 cut boundaries at offset 0, worst audio lag 0.0 ms.
- Beeps at known times through three cuts and a 44.1 → 48 kHz resample: within one sample.
- Source whose audio starts 278 ms after the video: worst audio error 0.06 ms.
- 20 transitions in 8 styles at 1080p: every frame present, every cut exact.
- A render with audio 10 ms late, a plan shifted by one frame, a zoom claimed 2 frames late, or
  a hard cut where the plan says crossfade: each is caught as FAIL.

## Safety

- Every path is checked against `[security] allowed_dirs` and the sensitive-file policy.
- The source file is never modified; outputs are never overwritten without `force`.
- Nothing is uploaded and no API keys are needed.

The same engine ships as a Claude Code skill in
[claude-skills `marketing/youtube-editor`](https://github.com/alirezarezvani/claude-skills).
