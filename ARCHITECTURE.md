# Architecture / v0.3

FILM UNIT is a local, file-based music-video compiler. Work and the director establish the story, art direction and approved shot inputs. The central path selects existing assets, compiles the entire song and preserves a new build. Generation is a separate workflow with its existing spend and approval controls.

## Sources of truth

| Source | Authority |
|---|---|
| `input/master.mp3` or `master.wav` | Original audio bytes and unshifted musical timeline |
| `input/lyrics.txt` | Exact lyric text; timing cannot substitute a transcription |
| `manifest/shots.json` | Visual order, absolute cut boundaries and direction |
| `lyrics/lyrics_timed.json` | Independent, source-bound cue timing and review |
| `builds/B####/build.json` | Captured selection, hashes, format, warnings and build identity |

Audio analysis yields measured duration, estimated tempo/beats/onsets and feature-change candidates. It does not identify sung words or invent verse labels. Absolute milliseconds map to frames with `frame_at(ms) = floor((ms * fps + 500) / 1000)`. Each clip receives the difference between mapped global boundaries. The source audio remains on its original timeline; its file hash is checked around compilation. AAC export is lossy even though source bytes remain unchanged.

## Core modules

| Module | Responsibility |
|---|---|
| `core.py`, `audio.py` | Atomic metadata, bounded paths, master hashes, production LOCK, frame clock, measured audio analysis |
| `production.py`, `presets/` | Neutral editable package, explicit water_please preset, beat-snapped draft cuts and technical storyboard slates |
| `schema.py` | Separate data versions; explicit, backed-up legacy metadata migration |
| `lyrics.py` | Exact source mapping, explicit repeats, reviewed cues, ASS/SRT, font coverage and burn-in |
| `timeline.py` | Split, merge, move and snap cuts without moving lyric cues |
| `resolver.py` | Hash-bound existing take selection and Final review binding |
| `compiler.py` | Preview/Final gates, local clip normalization/cache, complete-song mux and build capture |
| `builds.py` | Append-only build allocation, copies/inventory, verification and replay |
| `cli.py`, `app/control_panel.py` | Interfaces over the same engine; whole-song compile separated from advanced shot generation |

Existing `pipeline.py`, `renderers.py`, `openart_bridge.py`, `budget.py`, `takes.py`, `work_queue.py`, `economy.py`, `fal_renderer.py`, `benchmark.py`, `qc.py` and legacy `assemble.py` remain in place for compatibility. Economy/fal/benchmark are advanced experimental paths, not new dependencies of the local compiler. Their physical relocation is deferred.

## Preview and Final

Preview tries each shot's selected final video, selected draft video, first storyboard reference, then a labelled placeholder. Stale shot selections, missing files, hash mismatches, corrupt or unsuitable clips can fall through to the next source. Local normalization strips clip audio, respects source windows and assigns exact frame counts. It does not submit provider requests or convert still imagery into claimed character animation.

An always-compilable Preview means **missing creative assets do not block a structurally valid project**. It does not suppress a missing/changed master, invalid or gapped timeline, unavailable FFmpeg, unreadable configuration or filesystem failure. Those errors remain explicit. Missing or invalid lyric timing is reported and omitted instead of guessed. A completed Preview can contain zero valid lyric cues and unfinished visuals; `mode`, `preview_incomplete_shots`, `asset_counts`, fallback reasons and subtitle warnings distinguish it from a finished MV.

Final requires a current production LOCK that is not mock-only, complete reviewed source-faithful lyric cues, verified glyph coverage, and eligible assets for every shot. A final take needs reviewer/evidence bound to its bytes, shot hash, source window and current production fingerprint. An imported storyboard is eligible for an intentionally STATIC shot under the production LOCK. Drafts and placeholders do not satisfy Final. Naming an asset `final` or choosing final resolution alone grants no approval.

## Lyrics and subtitles

`prepare_lyrics()` mirrors the exact UTF-8 source into `lyrics/lyrics_source.txt`, parses source rows and creates an empty timing document. Source changes archive prior timing and clear its current review. Section headings are metadata. Empty repeated headings require explicit expansion to the full corresponding source section; the compiler does not silently duplicate or omit lyrics.

Cues reference source row IDs and optional character spans. Validation enforces unchanged text, integer-ms ranges within the song, ordered non-overlapping cues, source order and complete non-whitespace coverage. A review is bound to the exact source, repeat mapping and cues. The reviewer attests that these times match the actual vocal; no ASR, forced alignment or automatic listening verification is connected. Final currently requires lyrics; an instrumental/no-lyrics exemption is not implemented.

SRT preserves integer milliseconds. ASS rounds to centiseconds, records that limitation and uses the project canvas with at least 5% horizontal and 7% bottom margins. FontTools checks selected font cmap coverage for actual cue characters. Missing/unverified coverage warns in Preview and blocks Final. `subtitles.font_file` must stay inside the project when supplied. Fonts, source text, timing and subtitle reports are copied into each build. Safe-area margins and glyph coverage do not replace visual review of long lines, readability or subtitle placement.

## Immutable builds and local caching

A project mutex serializes compile work. Each attempt allocates a new `B####` directory; failure records `FAILED` and never overwrites a completed build. Inputs are independent byte copies, not hard links to mutable project assets. A build captures the selected raw sources, normalized timeline clips, original audio, creative documents and references, lyric source/timing, ASS/SRT and available font files.

The normalized clip cache is keyed by compiler version, shot hash, source hash, source window, selection kind and format. Cache bytes are hash-checked and media-probed. A new selection changes the affected key, while unrelated clips can be reused without rendering or purchasing them again. Clip cache entries are copied into the build before assembly.

A complete build records output and input SHA-256 inventory, shot-level source/clip hashes, review/LOCK provenance, warnings and Python/FFmpeg versions. `verify_build()` detects missing or changed inventoried files. `replay_build()` verifies that inventory and reassembles captured clips/audio/ASS in a new directory, without consulting live project files. Stored MP4s preserve historical bytes. A new encode on a different FFmpeg/font rendering environment is not promised to be bit-identical. This is an application-level append-only convention and integrity record, not signed or write-protected archival storage.

## Editing, locks and migration

Split preserves the left ID and allocates a new right ID. Merge accepts adjacent shots in the same sequence and retains the left direction/reference. Moving/snapping a cut preserves full-duration coverage and rejects sub-frame shots. Affected selections and QC state become stale; the production fingerprint requires renewed review. Timeline operations never rewrite lyrics or prior builds.

New projects declare project schema 3, shot schema 2, lyrics schema 1, build schema 1 and audio schema 1 separately from package 0.3.0. Explicit `migrate_project()` backs up original project metadata and updates version declarations. Legacy shot fields are compatible and remain byte-for-byte unchanged; missing optional fields use consumer defaults. Master files, storyboards, paid takes and LOCK records are preserved. Migration neither fabricates review nor authorizes changed production content. Unsupported future versions are rejected.

## Generation and QC retained

The prior generation path still binds production, output scope, renderer configuration, exact quote and retry policy to batch approval. Separate USD and credits pools, durable reservations, canonical paid-take IDs, raw take reuse and source-window review remain in force. Ambiguous provider submission is reconciled rather than resubmitted. Failed/pending jobs keep their reservations; refunds are not invented.

OpenArt remains a Work outbox/receipt image-to-video bridge, not an independent Python connection using extracted connector credentials. The existing pipeline may wait for generation or semantic review; the separate Preview compiler can meanwhile assemble available assets. Legacy generation exports in `output/` remain supported, but versioned whole-song outputs use `builds/`.

Technical QC and sample extraction exist. Semantic QC still reads explicit evidence-backed numeric review under the old schema; it is not an automatic neural evaluator. PASS/FAIL semantic review redesign and element/reference/start-end/video-edit capabilities are deferred. No additional provider was added for v0.3.

## Verification boundary

Tests include a real FFmpeg 240-second mixed-asset build, 82 synthetic timed lines, a one-shot replacement, unchanged source/audio/lyric checks, replay/tamper checks, strict Final gates, lyric validation, schemas and timeline editing. The long regression renders synthetic 320×240 media to constrain CI cost. It does not validate actual singer alignment or provider animation quality.

GitHub Actions is configured for Ubuntu with Python 3.11/3.12, FFmpeg, CJK fonts and pytest. Configuration is not evidence that a remote run passed; use the commit's Actions result. The control panel is local, with no hosted authentication, tenancy or background worker infrastructure.
