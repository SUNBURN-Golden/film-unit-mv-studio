# Architecture / v0.3

FILM UNIT is a local, file-based music-video compiler. Work and the director establish the story, art direction and approved shot inputs. The central path selects existing assets, compiles the entire song and preserves a new build. Generation is a separate workflow with its existing spend and approval controls.

## FRAME_ANIMATION_V1 scope exception (2026-09-30)

The descriptions below remain the implemented v0.3 / LEGACY_MV contract. The
User's [FRAME_ANIMATION_V1 adoption decision](docs/decisions/FRAME_ANIMATION_V1_ADOPTION_20260930.md)
adopts a separate development contract for the explicitly selected new mode:
browser Google OAuth and Drive archives; fixed-plan remote/subscription compose
and encode; qualified encoder drivers; immutable worker ranges in parallel with
one coordinator serializing state and build seals. LOCAL_FULL preserves copied
inputs and offline replay. DRIVE_BOUNDED preserves fixed object/member revisions
and hashes, provides online replay and explicit offline restore, and reports
external deletion or revoked access as an unavailable archive.

The [execution/storage design](docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md)
governs those exceptions for the new mode. ANIM-001 owns the detailed ADR, schema
consumers and migration boundaries before implementation. This decision does not
install these features, submit paid jobs, provision credentials or activate a
host. Existing projects, Build 1 and creative-generation approvals keep their
legacy contracts; mode and approval migration is explicit.

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

Final requires a current production LOCK that is not mock-only, complete reviewed source-faithful lyric cues, verified glyph coverage, and eligible assets for every shot. A final take needs reviewer/evidence bound to its bytes, shot definition, reference-image bytes, source window and shared visual context. The whole production fingerprint is provenance, not a video-review dependency. An imported storyboard is eligible for an intentionally STATIC shot under the production LOCK. Drafts and placeholders do not satisfy Final. Naming an asset `final` or choosing final resolution alone grants no approval.

Review dependencies have three layers. `visual_context_fingerprint()` hashes brief, source lyric text, story/style/character/location/directing bibles, shared character/location assets and format. Each video review adds only its own shot definition, references, clip bytes and source window. Lyric review independently hashes source/cues/repeats, subtitle settings, canvas/FPS and resolved font bytes/family. Production LOCK still covers the complete visual timeline, all references, lyrics revision and configured presentation.

Consequently lyric timing/font changes require lyric re-review and re-LOCK, while existing visual approvals survive. A cut edit invalidates its adjacent shots, not unrelated shots. Shared style changes invalidate all visual approvals. Changing one storyboard's bytes invalidates only its shot review, plus the global LOCK. The renderer QC path uses the same scope and checks its exact review binding before registering a normalized clip; stale QC cannot be rebound to newly edited inputs.

## Lyrics and subtitles

`prepare_lyrics()` mirrors the exact UTF-8 source into `lyrics/lyrics_source.txt`, parses source rows and creates an empty timing document. Source changes archive prior timing and clear its current review. Section headings are metadata. Empty repeated headings require explicit expansion to the full corresponding source section; the compiler does not silently duplicate or omit lyrics.

Cues reference source row IDs and optional character spans. Validation enforces unchanged text, integer-ms ranges within the song, ordered non-overlapping cues, source order and complete non-whitespace coverage. Review schema 2 additionally binds subtitle configuration and actual font identity. The reviewer attests that these times match the actual vocal; no ASR, forced alignment or automatic listening verification is connected. Final currently requires lyrics; an instrumental/no-lyrics exemption is not implemented. Legacy timing-only or globally bound video approvals are not automatically promoted to the new scope: their inputs lack the required scoped proof, so one explicit re-review is required. Saved historical builds remain intact.

SRT preserves integer milliseconds. ASS rounds to centiseconds, records that limitation and uses the project canvas with at least 5% horizontal and 7% bottom margins. FontTools checks selected font cmap coverage for actual cue characters. Missing/unverified coverage warns in Preview and blocks Final. `subtitles.font_file` must stay inside the project when supplied. Fonts, source text, timing and subtitle reports are copied into each build. Safe-area margins and glyph coverage do not replace visual review of long lines, readability or subtitle placement.

## Immutable builds and local caching

A project mutex serializes compile work. Each attempt allocates a new `B####` directory; failure records `FAILED` and never overwrites a completed build. Build ordering is numeric, including B10000 after B9999. Inputs are independent byte copies, not hard links to mutable project assets. A build captures the selected raw sources, normalized timeline clips, original audio, creative documents and references, lyric source/timing, ASS/SRT and available font files.

The normalized clip cache is keyed by compiler version, shot hash, source hash, source window, selection kind and format. Cache bytes are hash-checked and media-probed. A new selection changes the affected key, while unrelated clips can be reused without rendering or purchasing them again. Clip cache entries are copied into the build before assembly.

A complete build records output and input SHA-256 inventory, shot-level source/clip hashes, review/LOCK provenance, warnings and Python/FFmpeg versions. `verify_build()` detects missing or changed inventoried files. `replay_build()` verifies that inventory and reassembles captured clips/audio/ASS in a new directory, without consulting live project files. Stored MP4s preserve historical bytes. A new encode on a different FFmpeg/font rendering environment is not promised to be bit-identical. This is an application-level append-only convention and integrity record, not signed or write-protected archival storage.

## Editing, locks and migration

Split preserves the left ID and allocates a new right ID. Merge accepts adjacent shots in the same sequence and retains the left direction/reference. Moving/snapping a cut preserves full-duration coverage and rejects sub-frame shots. Affected selections and QC state become stale; the production fingerprint requires renewed review. Timeline operations never rewrite lyrics or prior builds.

New projects declare project schema 3, shot schema 2, lyrics schema 1, build schema 1 and audio schema 1 separately from package 0.3.0. Explicit `migrate_project()` backs up original project metadata and updates version declarations. Legacy shot fields are compatible and remain byte-for-byte unchanged; missing optional fields use consumer defaults. Master files, storyboards, paid takes and LOCK records are preserved. Migration neither fabricates review nor authorizes changed production content. Unsupported future versions are rejected.

## Generation and QC retained

The prior generation path still binds production, output scope, renderer configuration, exact quote and retry policy to batch approval. Separate USD and credits pools, durable reservations, canonical paid-take IDs, raw take reuse and source-window review remain in force. Ambiguous provider submission is reconciled rather than resubmitted. Failed/pending jobs keep their reservations; refunds are not invented.

OpenArt remains a Work outbox/receipt image-to-video bridge, not an independent Python connection using extracted connector credentials. The existing pipeline may wait for generation or semantic review; the separate Preview compiler can meanwhile assemble available assets. Legacy generation exports in `output/` remain supported, but versioned whole-song outputs use `builds/`.

`packets.py` writes per-shot hand-off packets (`render/packets/index.html` and `packets.json`) for making first frames and clips by hand in subscription apps. A packet holds the still-image prompt with the shot's cast/location reference images, the existing image-to-video prompt with a minimum clip length, and the existing import command. It contacts no provider, automates no consumer app and does not change shots, reviews, budgets or LOCK; imported results follow the same review and Final rules.

Technical QC and sample extraction exist. Semantic QC still reads explicit evidence-backed numeric review under the old schema; it is not an automatic neural evaluator. PASS/FAIL semantic review redesign and element/reference/start-end/video-edit capabilities are deferred. No additional provider was added for v0.3. Afterwards `gemini.py` added Nano Banana 2 first frames and a Veo 3.1 Lite image-to-video renderer. Frames use their own estimate and approval, bound to exact prompts and reference bytes, because they precede LOCK; video uses the existing LOCK-bound batch estimate. Both reserve USD in the shared ledger under `budget.max_usd`, write a job record before each POST, never resubmit an ambiguous request, treat a 4xx rejection as no job, send the API key only to the Gemini host and download clips within Google's 2-day retention. `providers.py` is the model-picker registry: three stages (director text, reference/frame images, video), each with interchangeable providers that declare cost class, fields and notes. Text providers share one OpenAI-compatible adapter plus the official Anthropic SDK (`max_retries=0`); image providers implement `ImageProvider` and run through `imagegen.py` (estimate, approval bound to exact inputs, job record before each request, free providers at cost 0 with no approval, paid ambiguity never resubmitted); video providers are the existing renderers. `settings.py` keeps keys in the user's settings folder (environment first, mode 0600), `safehttp.py` binds a key to its own host with no redirects or retries, and `director.py` drafts a proposal in two phases (world, then shot batches) that changes nothing until the User accepts it. The selection is stored in `project.yaml` under `providers`, which no fingerprint reads. `autopilot.py` runs only already-approved work in a single pass and stops at each human decision (frame approval, LOCK, video approval, Preview review); it has no wait loop.

## Verification boundary

Tests include a real FFmpeg 240-second mixed-asset build, 82 synthetic timed lines, a one-shot replacement, unchanged source/audio/lyric checks, replay/tamper checks, strict Final gates, lyric validation, schemas and timeline editing. The long regression renders synthetic 320×240 media to constrain CI cost. It does not validate actual singer alignment or provider animation quality.

GitHub Actions is configured for Ubuntu with Python 3.11/3.12, FFmpeg, CJK fonts and pytest. Configuration is not evidence that a remote run passed; use the commit's Actions result. The control panel is local, with no hosted authentication, tenancy or background worker infrastructure.
