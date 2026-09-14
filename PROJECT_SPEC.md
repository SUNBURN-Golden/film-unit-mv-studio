# Project specification / v0.3

## Goal and release boundary

Given a measured master track, authoritative lyric text and a full-song shot plan, compile the current cut at any stage, replace individual shots and retain every previous build. The director establishes the approved film; generation tools render particular shots and do not direct the whole MV.

v0.3 prioritizes lyrics/subtitles, Preview fallback, immutable builds and generic production defaults. Timeline edit operations and a four-minute regression/CI configuration accompany that core. Existing spend/LOCK/take-reuse safeguards remain. New provider integrations, element/reference video generation and semantic PASS/FAIL QC are deferred.

A rendered Preview is a review artifact, not proof of finished animation. A locked camera does not mean frozen subjects. LIMITED_MOTION and FULL_GENERATIVE need real approved motion in a finished animated production; local image hold/pan/zoom is a preview aid. STATIC is an explicit editorial choice, with no automatic savings quota. The compiler itself does not impose one animation style on all songs.

## Files and authority

| Path within `projects/<project>/` | Purpose |
|---|---|
| `project.yaml` | Output settings, budget, renderer settings, data schema declarations |
| `input/master.mp3` or `master.wav` | Original master, protected by SHA-256 |
| `input/brief.md`, `input/lyrics.txt` | Director brief and authoritative lyric source |
| `analysis/audio.json`, `waveform.png` | Measured audio features and visualization |
| `bible/`, `characters/`, `locations/` | Editable production direction and references |
| `manifest/sequence.json`, `shots.json`, `locks.json` | Full-duration visual plan and production approval |
| `manifest/assets.json` | Selected draft/final takes and exact review binding |
| `manifest/timeline_edits.json` | Visual edit events; lyrics are independent |
| `storyboard/` | First frames, contact sheet and review HTML |
| `lyrics/lyrics_source.txt`, `lyrics_timed.json`, `history/` | Source mirror, independent cues and superseded source-bound timing |
| `render/` | Retained renderer requests/state/takes, imported assets and local compile cache |
| `qc/` | Technical and semantic-review records |
| `builds/B####/` | A new build for each compile attempt |
| `migrations/` | Original metadata backup and explicit migration report |
| `output/` | Compatibility exports from the legacy renderer pipeline |

A completed build contains `MASTER_CLEAN.mp4`, `MASTER_SUBBED.mp4`, `lyrics.ass`, `lyrics.srt`, `lyrics_source.txt`, `lyrics_timed.json`, `subtitle_report.json`, captured font files, source snapshots, normalized timeline clips and `build.json`. It contains independent copies of selected inputs. `build.json` records source/clip hashes, selected source windows, asset counts, LOCK provenance, warnings, output format and file inventory. Failed attempts retain separate IDs and are not completed exports.

## Schemas and timing

| Data | Version |
|---|---|
| Project | 3 |
| Shot | 2 |
| Lyrics | 1 |
| Build | 1 |
| Audio | 1 |

These versions are independent of package version 0.3.0. Existing audio documents labelled `0.1` retain their established field semantics. Explicit migration backs up and updates project metadata, without rewriting approved production files. Legacy shot records remain compatible without a per-record schema field. Existing assets and lock bytes are preserved; future unsupported versions fail explicitly.

A shot requires `id`, `sequence`, `in_ms`, `out_ms`, `duration_ms`, `description`, `characters`, `locations`, `composition`, `camera`, `motion`, `references`, `render_mode`, `renderer`, `status`. New records also identify shot schema 2. IDs are unique S001-style values. Ranges use integer milliseconds, are contiguous and non-overlapping, cover measured duration and produce at least one frame. Render mode is STATIC, LIMITED_MOTION or FULL_GENERATIVE. Absolute boundary rounding prevents accumulating independent per-shot rounding errors.

Audio analysis includes duration, estimated BPM, beats/onsets, energy, candidate section boundaries, silence regions and peaks. Feature timestamps are measurement estimates, not word alignment or known verse/chorus annotations. The input limit remains ten minutes.

Lyric schema 1 stores source SHA, source rows, explicit repeat expansions and cues. Each cue has a unique ID, `source_row_id`, exact `text`, `start_ms` and `end_ms`; optional `char_start`/`char_end` split a source row into phrases. Every non-whitespace source span and explicit repeated span must be covered before review can approve Final. Timing must be ordered, non-overlapping and within the measured song. The system does not infer vocal timing from beat positions or equal divisions of the track.

## Compile contracts

| Condition | Preview | Final |
|---|---|---|
| Valid master hash, measured timeline, FFmpeg | Required | Required |
| Selected final video | Preferred; review may be pending | Must have current hash-bound review |
| Selected draft video | Used when final is unavailable | Rejected |
| Storyboard | Used when clips are unavailable | Only imported, deliberately STATIC shots under valid LOCK |
| Missing visual | Labelled placeholder | Rejected |
| Production LOCK | Can be absent/stale; recorded | Current and not mock-only |
| Incomplete or invalid lyric timing | Warning; valid available cues or no cues | Rejected |
| Missing/unverified font glyphs | Warning | Rejected |
| Provider calls during compile | None | None |
| Result location | New PREVIEW build | New FINAL build |

Corrupt or unusable selected media can fall through the Preview hierarchy. "Always compilable" does not hide invalid timeline structure, modified/missing audio or unavailable runtime/storage. A Preview with an empty subtitle track still produces both MP4 names and explicitly records incomplete lyrics; that file is not a finished lyric MV.

Default Final video is 4:3, 1440×1080, 24fps, H.264, CRF 18, yuv420p. Draft Preview limits width to 960 while preserving aspect ratio. Only the original master contributes audio; generated-clip audio is stripped. AAC 320kbps is a lossy derivative, with no loudness normalization or time stretch. JSON/SRT retain milliseconds; ASS rounds to centiseconds. Output duration is verified within frame/codec tolerance, not promised sample-exact as an MP4 container field.

Font coverage is measured against rendered cue characters. A full glyph-covering font can be supplied through `subtitles.font_file` relative to the project; `font_name`, `font_size`, `margin_x` and `margin_bottom` configure presentation. ASS uses minimum 5% horizontal and 7% bottom margins. Human preview review remains necessary for phrase length, line breaks and readability.

## Editing and reproducibility

Split, merge, move cut and snap-to-measured-beat/onset preserve full visual coverage. Merge is limited to neighboring shots in the same sequence and keeps the left direction/reference. Changed shots lose their selected asset authorization and need review; lyrics and completed builds remain unchanged.

Each saved build keeps the original selected assets as well as normalized per-shot clips, audio and subtitle/font inputs. Replaying a verified build needs no current project files. Historical exports retain their exact bytes; newly encoding them across FFmpeg versions is not guaranteed byte-identical. SHA inventories detect modified captured files but are not signed archival attestations. Local cache reuse avoids normalizing unchanged shot/source/window/format combinations again.

## Budget and generation boundary

The compiler Preview/Final functions call no generation provider. Existing advanced renderer operations still use exact quote approval, cumulative project reservations, separate credits/USD caps, bounded retries and ambiguous-submission recovery. Defaults remain 10,000 credits, USD 0 and two retries after the first attempt. They are project caps, not an account-wide subscription budget. No historical price snapshot is a live quote.

OpenArt remains an image-to-video Work bridge; Manual can import completed external work. Economy, fal and benchmark stay at their old import paths for compatibility and are experimental. Numeric semantic review remains evidence-driven and blocks unreviewed generated shots in the old pipeline. No new automatic vision evaluator or multi-reference generation capability is claimed.

## Acceptance and evidence

The v0.3 compiler regression fixture is a **240-second synthetic track, 48 shots, 7 selected final clips, 13 drafts, 25 storyboard images, 3 placeholders and 82 timed synthetic lyric lines**. It checks clean/subbed output duration and stream properties, then changes S024 and verifies a new build with the other 47 source hashes, audio hash and lyric timing unchanged. It uses 320×240, 24fps media for economical CI execution. Tests also cover fallback from corrupt/missing assets, invalid timeline rejection, strict Final gates, review binding, replay and tamper detection, lyrics, schemas and cut editing.

GitHub Actions is configured for Ubuntu, Python 3.11/3.12, FFmpeg, fonts and pytest. Remote success must be confirmed from an actual Actions run. See tests and the delivered execution report for what was run; configuration alone is not a PASS.

Earlier 30/60-second Mock tests remain recorded in `docs/ACCEPTANCE_REPORT.md`. Actual paid provider generations remain 0 in the established project evidence. The synthetic four-minute regression does not claim correct alignment of a real singer, AI animation quality, an approved 1080p song or the user's completed MV. The separately supplied song/artwork and their production review remain work at the project level, not prerequisites for exercising the generic compiler.
