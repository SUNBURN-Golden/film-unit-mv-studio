# CLAUDIA_PORT — claudia-bot investigation, port decisions, and the plan for sections 2–4

**Status: PHASE 1 of 2 — investigation and plan only.** No engine code is
changed by this document. Every claim below is marked as inspected source,
verified external fact (with URL), or planned work. Nothing here is a claim of
real external generation: `README.md:7` still applies — this repository has
zero verified real external AI video generations on record.

Source under investigation: `https://github.com/anabologyco-maker/claudia-bot`,
cloned to `/workspace/film-run/claudia-src`, inspected at `40584a4` (v0.3.4).
The clone is **not** part of this repository and nothing from it is vendored.

---

## 0. What claudia-bot actually is (verified, not README-level)

claudia-bot is an MIT-licensed MV-production toolkit (`LICENSE`, Copyright (c)
2026 anabology) driven by a `slop` Python CLI plus a Node delegation runner. It
was written for a specific production (the Claudia character's films — ESCAPE
VELOCITY, CLODYSSEY) and generalised afterwards. Three facts shape every port
decision:

1. **Its real code quality is high where it is local.** The modules worth
   porting are file-format and DSP work: forced alignment merging, beat maps,
   lip-sync measurement, storyboard pacing rules calibrated on a shipped film,
   coverage/cover-clip logic, spend ledgers, and a failure catalog
   (`docs/99-failures.md`) built from real production losses.
2. **Its generation path is API-key based.** `slop/gen/openrouter.py`,
   `slop/gen/higgsfield.py`, `slop/fetch.py` submit, poll and download through
   paid API providers. Film Unit's hard rules (no paid APIs, no API keys, no
   polling, no automatic retries) exclude this layer entirely.
3. **Its subscription-service path runs through a browser-driving agent** (the
   Grok bot pastes prompts into the owner's signed-in Midjourney/browser
   sessions — `docs/96-relay.md`, `docs/95-delegation.md`). Film Unit's own
   `docs/BROWSER_HANDOFF.md` documents why this is deliberately not done:
   consumer subscription terms forbid automated access (Midjourney ToS §1:
   "You may not use automated tools to access, interact with, or generate
   Assets" — https://docs.midjourney.com/hc/en-us/articles/32083055291277-Terms-of-Service;
   community guidelines repeat the ban —
   https://docs.midjourney.com/hc/en-us/articles/32013696484109-Community-Guidelines).
   Film Unit keeps the human-in-browser model.

Honesty note on verification depth: claudia-bot's test suite
(`tests/test_*.py`) uses synthetic fixtures and mocked HTTP; it does not
establish real paid-provider success. The production evidence lives in its
docs and per-film records — method claims below are "verified on their shipped
films" as documented, not re-verified here. Every ported method still needs a
real run in section 2 before being claimed as working.

### Licenses and bundled assets

| Item | License / terms | Port consequence |
|---|---|---|
| claudia-bot source | MIT © 2026 anabology (`LICENSE`) | May port code/methods; keep MIT notice on any copied code (planned: re-implement, attribute in module docstring + this file) |
| `engine/fonts/` bundled fonts | SIL OFL-1.1 notices retained | Not needed — Film Unit ships its own subtitle fonts; do not copy |
| `facebook/wav2vec2-large-960h-lv60-self` | Apache-2.0 | English-only aligner — replaced for Korean anyway (§1.4) |
| demucs code / `htdemucs` weights | MIT code; **weights carry no license tag** — claudia itself flags this (`slop/audio/stems.py` header) | Optional dependency only; document the flag; offer user-supplied stems as the alternative |
| CMUdict | BSD-2-Clause data | English pronunciations only — not ported (Korean path doesn't need it) |
| `wordfreq` | CC-BY-SA 4.0 | Exclude — attribution burden not worth it |
| Whisper / faster-whisper / whisper.cpp | MIT | Candidate dependency for Korean word timing (§1.4, §2) |
| Suno | Commercial use requires paid plan — https://help.suno.com/en/articles/9601665 | Only matters if the input song is Suno-generated; the film master is owner-supplied |

---

## 1. Investigation and port decisions

Classification legend: **reuse-as-is** (Film Unit already has it), **port-with-modification** (port the method/code, adapted), **design-reference-only** (copy the shape, not the code), **exclude**.

### 1.1 AI director ↔ production-tool wiring, resume, next-step execution — **port-with-modification**

- **Claudia code paths:** `slop/core_cli.py` (`status`/`next` over plain-file
  stage state), `slop/job.py` (background jobs with `progress.json`), `runner/deleg.mjs`
  (job-folder contract: `job.json` + `BRIEF.md` → detached agent →
  `status.json`, `outbox/`, `raw-N.jsonl`), `slop/relay.py` + `docs/96-relay.md`
  (verbatim inbox/outbox handoff to the chat relay), `docs/95-delegation.md`,
  `skills/mv-studio/SKILL.md`, `skills/mv-director/SKILL.md` (gate list G1–G6,
  `docs/00-overview.md` §4).
- **Problem it solves in Film Unit:** today `engine/autopilot.py` stops at
  gates, but heavy reasoning steps (conti drafting, prompt-book writing, review
  summarising) happen as chat copy-paste with no durable job record. Claudia's
  job-folder contract is exactly the "AI director" wiring the goal describes:
  one folder = one delegated unit of work, resumable, with raw logs kept.
- **Port:** adopt the *contract*, not the runner. One new, thin module
  (planned `engine/director_job.py`) that writes `jobs/<id>/BRIEF.md`, records
  `jobs/<id>/status.json` (CREATED/USER_RUNS/DONE/FAILED) and ingests
  `jobs/<id>/outbox/` when the user drops the result back — the user runs their
  own Claude Code / Gemini CLI / web chat on the brief. No daemon, no polling,
  no new state system: a job folder is the same "plain files + explicit state"
  pattern Film Unit already uses under `qc/` and `render/`.
- **Dependencies:** none (plain files; the agent is the user's own
  subscription client).
- **Verification:** unit test on a fixture job; real verification is the §3
  scene, where the conti for the hard cut is produced through a job folder.
- **vs existing Film Unit:** `engine/autopilot.py` gate table +
  `app/execution_console.py` remain the orchestration authority; the job
  folder only packages "work to be done outside". `docs/BROWSER_HANDOFF.md`
  already describes the copy/paste equivalent — this formalises it for
  multi-step delegated work.
- **License/terms:** MIT method port. Claude Code is included in Claude Pro/Max
  subscriptions (https://claude.com/pricing/,
  https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan);
  the user picks which subscription agent runs a brief — nothing is launched
  or driven automatically.

### 1.2 Character appearance consistency, reference image & prompt management — **port-with-modification**

- **Claudia code paths:** `templates/project/project.json` (`lead.canon`,
  `lead.far`), `docs/50-plates-midjourney.md` §5 (identity is pinned **text** —
  never a face image for the lead; ethnicity/age/skin words mandatory after 12
  plates drifted), fixed clause order (framing → canon → placement → pose →
  set → empty-field-for-type → style tail → `no text, no letters, no logos`),
  4-test-plates gate (G3) before any batch, `plates/picks.json` + `slop/board/plates.py`.
- **Problem it solves in Film Unit:** `engine/packets.py` and
  `engine/conti_brief.py` already build prompts from `bible/characters.yaml`
  with reference images and input dispositions. What is missing is (a) a
  pinned *canon text* field per character with a short "far" variant for wide
  shots, and (b) the fixed clause ordering that Claudia's records show prevents
  the set swallowing the character.
- **Port:** add `canon`/`far` text fields to character bible entries, plumb
  them through `packets.py`/`conti_brief.py`, reorder clause assembly, keep
  reference images as the *declared inputs* they already are (canon text is
  additive, not a replacement — reference images still ride the handoff where
  the service supports them).
- **Adaptation guard (task §3):** the canon text is authored from the user's
  bible entry and style bible — **no Claudia canon, no photoreal default, no
  MJ parameter grammar** (`--ar`, `--raw`, personalization profile are MJ-only
  and MJ isn't even an allowed path here). The same fields carry 2D animation
  identity ("the short canon for a 2D cel-style lead" is just different text).
- **Dependencies:** none — schema extension + prompt assembly change.
- **Verification:** prompt-snapshot unit tests (existing style) asserting
  clause order and canon presence; real check = the §3 scene's first-frame
  candidates keep the character across the take.
- **License/terms:** MIT. Midjourney-specific guidance excluded with MJ itself.

### 1.3 Real image/video generation request → result retrieval → applied to shot — **design-reference-only**

- **Claudia code paths:** `slop/gen/clips.py` (clip records, `submit/poll/ingest`,
  per-clip takes), `slop/gen/images.py`, `slop/gen/openrouter.py`,
  `slop/gen/higgsfield.py`, `slop/fetch.py`, `docs/60-clips.md`.
- **Problem it solves in Film Unit:** Film Unit's equivalent already exists and
  is *stricter*: `engine/handoff.py` (brief → user generates in their browser →
  import → validate → `render/handoff_log.json`), `engine/segment_gen.py` (job
  journal, UNKNOWN fencing, one-identity-per-submission),
  `engine/takes.py` (content-keyed paid-take reuse = duplicate-charge
  prevention), `engine/conti_brief.py` (`check_take` with mechanical checks +
  contact sheet).
- **Design-referenced:** immutable per-shot take records keyed by content hash;
  clip-length snapping to what the service makes (`conti_brief.CLIP_STEPS`
  already does Flow's 4/6/8 s); never overwrite a take; the sung-clip vs
  motion-clip prompt split (§1.4).
- **Excluded:** every line of provider network code — submit/poll/ingest,
  price-quoting adapters, async job polling. Conflicts with hard rules.
- **Dependencies (of what remains):** none beyond existing modules.
- **Verification:** existing handoff/segment tests; real run in §4.
- **vs existing Film Unit:** manual handoff is already the sanctioned path;
  this area adds no new engine, only brief templates and ledger fields.
- **License/terms:** provider ToS gate everything — see service matrix §3.3.
  Grok Imagine API (https://x.ai/api/imagine, from $0.02/s) and
  OpenRouter/Higgsfield are **out** (paid API); Grok Imagine on the user's own
  subscription (https://x.ai/pricing) is in, via manual handoff.

### 1.4 Audio analysis, lyric alignment, beat-synced editing, lip-sync review — **port-with-modification** (highest impact)

- **Claudia code paths:** `slop/audio/stems.py` (demucs vocal stem, CPU),
  `slop/audio/align.py` + `merge.py` + `pron.py` (wav2vec2 CTC Viterbi forced
  alignment, merged with Whisper where confidence < 0.5, energy-rise start
  correction, interpolated unplaced words, ear-check list in
  `analysis/report.md`), `slop/audio/beatmap.py` (downbeat phase, sections,
  hits), `slop/board/draft.py` (cut-on-beat + per-section pacing measured on a
  shipped film: intro 1.33 s/shot, late chorus 1.67, outro 4.4, …),
  `slop/sync/avsync.py` (lip-sync = *audio* check: wave NCC + mel cosine vs the
  vocal reference, global lag, divergence point, cut suggestion on last beat),
  `slop/sync/coverage.py` (sung-seconds coverage → cover-clip proposals →
  cut-away fallback), `docs/20-analysis.md`, `docs/61-lipsync.md`.
- **Problem it solves in Film Unit:** `engine/audio.py` today produces
  RMS/onset/beat/centroid data and stops; `engine/lyrics.py` requires fully
  manual cue timing. There is no word-level alignment at all — so "lyrics move
  with the song" is human labour, and lip-sync review doesn't exist. This is
  the largest real gap to the goal.
- **Port:**
  1. **Korean word timing** (planned `engine/align.py`): Whisper word-level
     timestamps on the vocal band/stem, merged per claudia's `merge.py` rules
     (confidence threshold → energy-rise correction on long words → interpolate
     unplaced words inside a line) producing *proposed* cues for
     `lyrics/lyrics_timed.json` plus an ear-check list. **Never auto-approved**
     — the existing review fingerprint flow stays the gate.
     *Honesty:* claudia's own aligner is English-only (wav2vec2-960h + CMUdict
     spell A–Z only); the method ports, the recognizer must be swapped to a
     Korean-capable one — Whisper (`whisper`/`faster-whisper`, MIT; Korean is a
     supported language) on the isolated vocal. This is why "English alignment
     ≠ Korean support" is a real engineering item, not a checkbox.
  2. **Vocal stem** (`engine/audio.py` extension): optional demucs import of
     the claudia stems approach, OR user-supplied stem export; fallback is a
     librosa harmonic/vocal-band proxy (already have librosa). htdemucs weight
     license flag documented.
  3. **Sync measurement** (planned `engine/sync_check.py`): port `avsync.py`
     wholesale — it is pure numpy/scipy/FFmpeg, already the deps Film Unit has.
     For an imported take *that carries its own audio*, measure lag/divergence
     against the shot's vocal reference; store the result in the `check_take`
     record. *Honesty:* this detects "the model copied our vocal" sync —
     exactly what Seedance reference mode did. Veo 3.1 in Flow does not copy a
     supplied vocal; Grok Imagine 1.5 has voice references
     (https://docs.x.ai/developers/model-capabilities/video/overview). So
     sync-measure is capability-gated: silent/motion clips get cut-structure
     checks only.
  4. **Beat-synced editing:** `board/draft.py`'s measured pace table and
     cut-on-beat/word rules port as *advisory scoring* into the conti/shot
     stage — the Film Unit conti stays authoritative, but its draft pace gets
     Claudia's measured numbers instead of a guess.
- **Dependencies:** `whisper` or `faster-whisper` (optional extra; tests mock
  its output), `demucs` optional, numpy/scipy/librosa/soundfile already present.
- **Verification:** synthetic-audio unit tests (align on a generated tone track
  with known onsets; avsync on shifted copies — Claudia's own test style);
  real verification = the Korean-song run in §3.2.
- **License/terms:** MIT code paths; whisper/faster-whisper MIT; demucs MIT +
  flagged weights; wav2vec2/CMUdict not ported.

### 1.5 Candidate comparison, partial revision, user feedback application — **port-with-modification**

- **Claudia code paths:** `plates/cands/<plate>/<k>.jpg` + `picks.json` (4
  candidates, owner picks, rejects stay as a free pool), per-clip multiple
  takes with measured sync (`docs/60-clips.md` takes table), coverage → cover
  clip → cut-away ladder (`slop/sync/coverage.py`, `docs/61-lipsync.md`),
  "fix the source, never the frame" rule (`docs/50-plates-midjourney.md` §1.13),
  `docs/99-failures.md` failure catalog.
- **Problem it solves in Film Unit:** `engine/takes.py`, `engine/builds.py`,
  shot replacement, and `check_take`'s PENDING criteria already cover the
  mechanics. Missing: (a) an explicit **revision instruction record** — user
  wording verbatim → changed prompt → new take, linked to the rejected one
  (Claudia stores owner wording verbatim in `inbox.jsonl`; the traceable
  instruction→prompt→take chain is what partial revision needs); (b) the
  cover/cut-away decision ladder for failed sung spans.
- **Port:** a `revision` record per shot (instruction text, resulting prompt
  diff, take ids, timestamps) under `qc/conti/<shot>/`, consumed by the
  existing replace-shot path. The ladder (keep synced span → cover clip → cut
  away) becomes an advisory in the check record, not automation.
- **Dependencies:** none new.
- **Verification:** unit test on the record + the §4 revision test end-to-end.
- **vs existing Film Unit:** all state lands in existing dirs; no parallel
  candidate DB.

### 1.6 Role split of subscription services / APIs / local execution — **design-reference-only**

- **Claudia code paths:** `docs/ARCHITECTURE.md`, `docs/96-relay.md`,
  `README.md` service table — Claude Code/Opus (director, local files), Grok
  bot (relay + browser executor), user's Midjourney/Suno (manual), OpenRouter/
  Higgsfield (paid API), local FFmpeg/Python.
- **Film Unit's split (kept):** director/reasoning = the user's own Claude
  Code/Gemini CLI/Claude web subscription via job-folder briefs and copy-paste
  (§1.1); image generation = user's browser, their subscription app (Gemini
  image, etc.) via `handoff.py`; video = user's browser, Flow/Veo or Grok
  Imagine or any subscription app the user picks; all analysis/edit/compose =
  local Python+FFmpeg. **No API stage exists in the design.**
- **Design-referenced:** the *explicit* per-service capability table — Film
  Unit's `templates/` dir already seeds this (`pixverse_v6_image2video.json`,
  `seedance_fast_image2video.json` …). Port = write the matrix for the
  subscription tier (Flow clip lengths, Grok Imagine ≤15 s + references,
  Gemini image) into handoff briefs so the user sees "this shot wants a 6 s
  clip; your app X makes it".
- **Excluded:** the Grok browser executor — automating the owner's signed-in
  consumer accounts is banned by the same ToS class as Midjourney's above and
  by Film Unit's own handoff doc.
- **Dependencies/terms:** verified current sources in §3.3.

### 1.7 Cost control and production failure recovery — **port-with-modification** (small port)

- **Claudia code paths:** `templates/project/project.json` budget block +
  `confirm_over_usd`, `slop spend` ledger reading provider-reported usage,
  estimate-before-submit, `spend --freeze`, `docs/99-failures.md`.
- **Problem it solves in Film Unit:** `engine/budget.py` (estimate → exact
  approval → reserve; worst-case cap; failed stays reserved),
  `engine/segment_gen.py` (SUBMIT_INTENT journal before any side effect;
  UNKNOWN fencing) already enforce the *paid* side. The hole: **manual
  generation has no ledger** — a user burning Flow credits on retries isn't
  visible to the project.
- **Port:** add optional `subscription_units` (e.g. "Flow credits"),
  `attempt_no`, `human_minutes` fields to `handoff_log`/`check_take` records —
  user-entered at import time; a `render/manual_spend.json` roll-up so the
  §4 report's "real cost" is real. Also port `docs/99-failures.md` as a Film
  Unit failure checklist adapted to manual mode (stale tab, wrong file
  imported, download folder mis-assignment, silent provider decline →
  user-visible "still NOTHING_IMPORTED" rather than a hung state).
- **Dependencies:** none.
- **Verification:** unit test on ledger rollup; §4 report is the proof format.
- **Duplicate-charge prevention:** unchanged — content-keyed takes
  (`engine/takes.py`) mean re-importing the same file is idempotent.

### 1.8 Explicitly excluded (whole components)

| Claudia component | Why excluded |
|---|---|
| `slop/relay.py` chat relay (Grok bot inbox/outbox) | Requires the Grok bot + account automation; no driver available in Film Unit |
| `slop/gen/{openrouter,higgsfield}.py`, `slop/fetch.py`, paid adapters | Hard rule: no paid APIs, no keys |
| Grok-as-browser-executor (Midjourney paste loops) | ToS automation bans (§1.0.3, MJ ToS link above); Film Unit handoff policy |
| `slop/taste/` (arena, embedding server, tunnels) | Whole subsystem; out of scope for the hard cut — revisit if taste iteration becomes the bottleneck |
| `runner/deleg.mjs` as shipped (engine spawning, env scrubbing, tgz pack/unpack, `wire`) | Claude Code/Gemini CLI runtime assumptions + supervisor; Film Unit ports only the folder contract (§1.1) |
| Claudia canon content (`project.json lead.canon` text, claudia.gallery MCP) | Task §3: Claudia character and photoreal direction must not be defaults |
| Midjourney prompt grammar (`--ar`, `--raw`, profiles, srefs) | Service-specific to a service not in the allowed set |
| `wordfreq`-dependent text stats | CC-BY-SA burden |

---

## 2. Implementation plan (owner-task section 2) — the chosen path

**Chosen path: the REDESIGN_W00 "one hard real cut".** One 5–10 s sung scene of
the real Korean song, driven end to end inside Film Unit:

```
input (master audio + lyrics.txt + character bible + direction)
  → analyze (existing engine/audio.py + vocal stem + beat map)
  → Korean word alignment (new engine/align.py → proposed cues)
  → reviewer confirms cues (existing lyrics review fingerprint)
  → conti/brief for the scene's shots (conti_brief + canon fields + pace rules)
  → manual generation (user's browser: image app for first frame →
     Flow/Veo or Grok Imagine for the clip) via engine/handoff.py briefs
  → import → mechanical checks + sync-measure + vision-judge brief
     (engine/conti_brief.check_take + new engine/sync_check.py)
  → accept take → ONE revision cycle (new §1.5 revision record → new take →
     replace shot)
  → compile Preview (existing compositor: real audio + exact ASS/SRT
     subtitles) → Final after locks
```

### Files to change or reuse (phase 2)

| File | Change |
|---|---|
| `engine/align.py` **(new)** | Whisper-based Korean word timing → proposed cues + ear-check list; output shaped to `lyrics_timed.json`; method ported from `slop/audio/merge.py` (confidence → energy-rise → interpolate), recognizer swapped |
| `engine/sync_check.py` **(new)** | Port of `slop/sync/avsync.py` (numpy+FFmpeg only): lag, synced span, divergence, suggested cut; writes into `qc/conti/<shot>/` records |
| `engine/audio.py` | Add optional vocal stem (demucs if installed / user-supplied stem / vocal-band proxy); master SHA protection unchanged |
| `engine/packets.py`, `engine/conti_brief.py` | Character `canon`/`far` fields; fixed clause order; sung-clip vs motion-clip prompt grammar; per-service capability notes in briefs |
| `engine/handoff.py` | Brief gains "which app, expected clip length, what to download"; import gains optional `subscription_units`/`attempt_no`/`human_minutes` → `render/handoff_log.json` |
| `engine/manual_spend.py` **(new, ~100 lines)** or fold into handoff | Roll-up ledger for manual units + human minutes |
| `engine/revision` records | Extend `conti_brief` check-in with instruction→prompt→take chain (no new module if it fits there) |
| `app/handoff_ui.py`, `app/brief_board.py` | Surface the new brief fields and ledger entry points |
| `engine/autopilot.py`, `engine/builds.py`, `engine/compositor.py`, `engine/lyrics.py`, `engine/takes.py`, `engine/budget.py` | **Reuse as-is** — gates, immutable builds, exact-timing subtitles, take dedup, caps all stay |
| `tests/` | Fast synthetic tests only: alignment on fixture word timestamps, avsync on shifted WAVs, ledger rollup, prompt-clause snapshots. No network, no model downloads in CI |

**No duplicate engine/state:** every new artifact lands in existing dirs
(`analysis/`, `qc/conti/`, `render/`, `lyrics/`) under existing schemas; no new
job queue, no new takes DB, no second state machine — autopilot gates and the
job-folder contract reuse the plain-file convention already in use.

---

## 3. Adaptation plan (owner-task section 3)

### 3.1 Characters and 2D, not Claudia and photoreal

- Canon/far text and every style word come from the project's own
  `bible/characters.yaml` + style bible. The Claudia canon text is excluded
  (§1.8). A 2D-animated lead is expressed by canon text + style tail + first-frame
  reference — the same fields photoreal would use, different content. Tests
  will include a 2D-style bible fixture so "photoreal" can never be the
  hardcoded default.

### 3.2 Korean real-song verification plan

Subject: the owner's real Korean-language master (the film's song), supplied
via `input/`. Steps and pass criteria:

1. `analyze` on the master — beat map + tempo; master SHA unchanged (asserted).
2. `align` — Whisper word timings over the vocal band → proposed cues +
   ear-check list. **Pass:** the ear-check list exists; user confirms ≥ the
   sampled line starts; unresolved rows remain flagged, never guessed.
3. Scene = first sung window 5–10 s (verse-1 or hook opening the analysis
   picks), one character, 2D look per project bible.
4. Briefs → user generates first frame + clip in their subscription app →
   import → checks → judge brief → accept.
5. One revision (§4) → new take → replace shot.
6. Compile Preview; verify: master audio SHA identical, `lyrics_timed.json`
   cue bytes identical, prior build dirs intact, subtitle font verified.
7. Report per §4 format.

### 3.3 Service matrix (verified 2026-10; re-verify at implementation time)

| Role | Service (user's subscription) | Verified facts | Source |
|---|---|---|---|
| Director / reasoning / vision judge | Claude Code on Pro/Max, or Gemini app | Claude Code included in all paid Claude plans | https://claude.com/pricing/ , https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan |
| First frame / stills | Gemini app image generation | included in Google AI plans | https://one.google.com/about/google-ai-plans/ |
| Video clip | Google Flow (Veo 3.1) | Clips 4/6/8 s; Pro +1,000 credits/mo, Ultra +10,000/25,000; Veo 3.1 Fast = 20 credits/gen (10 on Ultra) | https://support.google.com/flow/answer/16526234 , https://support.google.com/flow/answer/16893917 , https://one.google.com/about/google-ai-plans/ |
| Video clip (alt) | Grok Imagine on SuperGrok tier | ≤15 s clips, up to 14 image + 3 voice refs on video-1.5 | https://x.ai/pricing , https://docs.x.ai/developers/model-capabilities/video/overview |
| Song provenance | Suno (if input is Suno-made) | commercial use needs paid plan | https://help.suno.com/en/articles/9601665 |
| Excluded | OpenRouter, Higgsfield, fal, xAI API, Midjourney automation | paid APIs / automation bans | https://docs.midjourney.com/hc/en-us/articles/32083055291277-Terms-of-Service |

### 3.4 Preservation guarantees (all existing mechanisms, asserted in §3.2 step 6)

Original audio SHA / lyric timing (`timing_fingerprint` — shots can't move
cues) / shot replacement (`builds.py` history) / past-build preservation /
budget cap (`budget.py`) / duplicate-charge prevention (`takes.py` content
keys) / UNKNOWN fencing (`segment_gen.py`) — all unchanged; internal
verification automatic, user sees outputs + fix candidates + needed decisions.

---

## 4. Done-criteria plan (owner-task section 4) and risks

**Scene:** the analysis-chosen first sung window of the real Korean song —
one 4/6/8 s Flow clip (or equivalent), one character, one camera move,
2D style per bible. This is the "usable in the real film" unit.

**Revision test:** one user instruction (e.g. "look to camera earlier" /
"more wind in hair") → recorded verbatim → new brief → user regenerates →
import as take 2 → replace shot → compile build N+1 → assert build N,
lyrics timing, and audio untouched.

**Report format (to be filled with real numbers in phase 2):**

| Field | Source |
|---|---|
| Before/after outputs | build dirs + take files + hashes |
| Human intervention | count of handoffs, browser sessions, minutes |
| Elapsed wall time | job/brief/check timestamps |
| Real cost | `manual_spend.json` units (subscription credits) + $0 marginal cash |
| Failures | every fenced/UNKNOWN/failed import, verbatim |
| Unsupported items | anything still not producible |
| Test-pass counts | pytest tallies — **separate from production success** |

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| Whisper word timing drifts on sung Korean (melisma, ad-libs) | ear-check list + energy-rise correction + existing manual cue editor; unresolved stays flagged |
| Flow doesn't copy the vocal → lip-sync can't be measured as audio-copy | capability-gated: sync-check only runs on audio-carrying takes expected to copy; otherwise vision-judge + cut structure; say "unmeasurable" rather than fake a number |
| Clip length quantization (4/6/8 s) vs shot length | `conti_brief.clip_length` snapping already exists; brief prints the mismatch |
| Flow monthly credit ceiling (Pro: 1,000/mo; ~20/gen Fast) | ledger + worst-case estimate before the user starts; scene chosen to fit in a handful of generations |
| Character consistency across takes in 2D | canon text + reference images + contact-sheet comparison; human eyeballs stay the final gate |
| htdemucs weight licence unclear | stems optional; user-supplied stem export supported; flag printed |
| No polling means the engine can't know a generation finished | by design — UNKNOWN stays fenced; import is the completion signal |
| Real browser generation needs the owner, not CI | phase 2 reports honestly what ran vs what was prepared |

### What this document deliberately does NOT claim

No real generation has happened. Test-pass counts ≠ production success.
Section 2's path is planned end to end; its proof is the phase-2 run on the
real Korean song. If any generation step proves impossible within existing
subscriptions, that step will be reported plainly and everything else
(analysis, alignment, briefs, review, composition) delivered with real local
tools — FFmpeg included — per the hard rules.
