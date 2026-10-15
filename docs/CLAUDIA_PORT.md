# CLAUDIA_PORT — claudia-bot investigation, port decisions, and the plan for sections 2–4

**Status: PHASE 2 — built and run on a real Korean song (§5).** Sections 0–4
are the reviewed plan (kept as reviewed; where the build deviated, §5.2 says
so). §5 is the phase-2 report: what was built, the real run, before/after
outputs, human intervention, time, cost, failures and unsupported items.
Nothing here is a claim of real external AI generation: `README.md:7` still
applies — the image/video generation step was **not** performed (§5.5).

Revision 2 (2026-10-14), after the r1 plan review: the manual-generation cost
ledger is reclassified **reuse-as-is** (`qc/conti/` records + `conti-metrics`
already exist and are tested — §1.7) and the planned `manual_spend` module,
`handoff_log` cost fields and `jobs/<id>/` status tree are dropped as
duplicates; `look` is reused as the canon field (§1.2); Korean alignment is
re-scoped to line-level cues with the Hangul matching work named (§1.4); the
REDESIGN_W00 R3 `production` block dependency is made explicit (§2).

Revision 3 (2026-10-15), after the r4 plan review (no blocking findings; its
four non-blocking corrections applied): conti answers live in
`conti/<shot>.json`, not `qc/conti/`, and persisted requests are placed and
named so `_records`' `*.json` scan never counts them as takes — the §1.5
revision record moves inside the take's check record for the same reason
(§1.1, §1.5); `look` is optional and the `far` plumbing tolerates its absence
(§1.2); the MMS_FA spike names its uroman step, torch weight and **CC-BY-NC
4.0** weight licence (§1.4, licence table); the R2 ✅ cross-reference points
at REDESIGN_W00 §5 (§1.7).

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
   coverage/cover-clip logic, and a failure catalog (`docs/99-failures.md`)
   built from real production losses. (Its spend-ledger *shape* is already
   matched by Film Unit's own `qc/conti/` ledger — §1.7 reuses that.)
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
| Whisper / faster-whisper / whisper.cpp | MIT | Candidate dependency for Korean lyric timing (§1.4, §2) |
| torchaudio `MMS_FA` bundle weights | **CC-BY-NC 4.0** (non-commercial) — https://docs.pytorch.org/audio/2.11.0/generated/torchaudio.pipelines.MMS_FA.html | Spike only (§1.4); not shipped unless the owner rules the film's use acceptable under NC terms — otherwise the spike result is reference-only |
| uroman (romaniser for MMS_FA input) | MIT — https://github.com/isi-nlp/uroman | Needed only by the MMS_FA spike |
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
- **What Film Unit already has:** the paste-out → paste-back contract with
  *stored answers* exists — `engine/conti.py` `conti-request`/`conti-submit`
  (answers validated and stored as `conti/<shot>.json` — `CONTI_DIR`,
  `conti.py:21,233`), `engine/conti_brief.py`
  `conti-judge-request`/`conti-judge-submit` (judge answers land in the
  `judgements` of the take's check record `qc/conti/<shot>/<take sha16>.json`
  — `QA_DIR`, `conti_brief.py:37,202-203`), and `engine/director.py`'s
  proposal + log under `bible/`. `engine/autopilot.py` +
  `app/execution_console.py` list what remains to be done.
- **The real gap is narrower:** the *request text itself* is not persisted.
  `conti_request`/`judge_request` return text to paste; only the answer lands
  on disk — so a discussion resumed in a new session loses the exact request
  that produced an earlier answer.
- **Port:** extend the existing request/answer convention, do not add a second
  one. Claudia's `jobs/<id>/` tree with its own CREATED/USER_RUNS/DONE/FAILED
  status would be a parallel convention over state Film Unit already expresses
  (records on disk + autopilot's gate table) — dropped. Instead, each
  `*-request` output is also written to disk, as the exact returned text,
  next to where its answer is stored:
  - **conti request** → `conti/requests/<request sha16>.txt`. One
    `conti_request` call can cover several shots, so the file is keyed by the
    text's hash, not a shot id (the shot ids are inside its payload). Nothing
    scans `conti/` — `conti.py` opens `conti/<shot>.json` by path only. The
    answer links back through one optional `request_sha256` key that
    `conti-submit` sets when given the request id; `KEYS` is a closed set
    (`conti.py:26`, enforced at `:106`), so this is a one-key schema
    extension, not a second record.
  - **judge request** → `qc/conti/<shot>/<take sha16>.judge-<request sha16>.txt`,
    beside the check record whose `judgements` its answer fills. Deliberately
    **not** `*.json`: `_records` (`conti_brief.py:206-208`) loads every
    `*.json` in that folder as a take check record, and `conti-metrics`
    (`:416`) counts them as attempts.
  - **hard cut only** — both are also copied to `docs/evidence/hard-cut-01/`,
    where REDESIGN_W00 R4 keeps request and answer.

  So any answer can be re-read against the request that produced it and a
  multi-session discussion resumes from files. The user still runs their own
  Claude Code / Gemini CLI / web chat on the request text. No daemon, no
  polling, no new state system.
- **Dependencies:** none (plain files; the agent is the user's own
  subscription client).
- **Verification:** unit test that the persisted request bytes equal what
  `conti_request`/`judge_request` return, and that `conti-metrics` attempt
  counts and `load_check` results are unchanged after a judge request is
  persisted; real verification is the §3 scene,
  where the conti for the hard cut is produced through persisted
  request/answer pairs.
- **vs existing Film Unit:** `engine/autopilot.py` gate table +
  `app/execution_console.py` remain the orchestration authority; the
  persisted request only packages "work to be done outside".
  `docs/BROWSER_HANDOFF.md` already describes the copy/paste equivalent —
  this formalises it for multi-session discussions.
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
- **What Film Unit already has:** `engine/packets.py` and
  `engine/conti_brief.py` already build prompts from `bible/characters.yaml`
  with reference images and input dispositions. The pinned identity text
  already has a field: `look` ("fixed visual traits repeated in every shot"),
  defined by the director proposal schema (`engine/director.py:27`, validated
  at `:101`) and read with `invariants` by `packets.py`'s `entry_text`
  (`:59-63`). It is **not** universally present — the shipped preset
  `presets/water_please/characters.yaml` carries only
  `invariants`/`behavior` — and `entry_text` already skips a missing `look`.
  **Reuse `look` as canon** — no duplicate `canon` field is added.
- **What is missing:** (a) a short "far" variant of that identity text for
  wide shots, and (b) the fixed clause ordering that Claudia's records show
  prevents the set swallowing the character.
- **Port:** add only the `far` text field to character bible entries; plumb
  `look`/`invariants`/`far` through `packets.py`/`conti_brief.py` and reorder
  clause assembly to Claudia's fixed order (identity text placed before the
  set); keep reference images as the *declared inputs* they already are
  (canon text is additive, not a replacement — reference images still ride
  the handoff where the service supports them). Both `look` and `far` are
  optional, as `entry_text` already treats `look`: a wide shot uses `far`,
  else `look`, else `invariants` alone, and an entry with none of them gets
  a brief warning ("no identity text — character may drift") rather than an
  error, so preset bibles keep working unchanged.
- **Adaptation guard (task §3):** the canon text is authored from the user's
  bible entry and style bible — **no Claudia canon, no photoreal default, no
  MJ parameter grammar** (`--ar`, `--raw`, personalization profile are MJ-only
  and MJ isn't even an allowed path here). The same fields carry 2D animation
  identity ("the short canon for a 2D cel-style lead" is just different text).
- **Dependencies:** none — schema extension + prompt assembly change.
- **Verification:** prompt-snapshot unit tests (existing style) asserting
  clause order and identity-text presence, plus one on the `water_please`
  preset shape (no `look`, no `far`) asserting the brief still renders with
  the warning; real check = the §3 scene's
  first-frame candidates keep the character across the take.
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
  this area adds no new engine, only brief-text additions.
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
  1. **Korean lyric-line timing** (planned `engine/align.py`): *proposed* cues
     for `lyrics/lyrics_timed.json` plus an ear-check list. **Never
     auto-approved** — the existing review fingerprint flow stays the gate.
     Film Unit cues are line-level (`engine/lyrics.py` emits SRT/ASS from
     `cues[].start_ms/end_ms/text`), so the realistic target is each lyric
     line's start/end, not per-word karaoke. Three pieces, honestly labelled:
     - **Recognizer — new code, not a port:** local Whisper
       (`whisper`/`faster-whisper`, MIT; Korean supported) word timestamps on
       the isolated vocal. Claudia's own Whisper path is a paid OpenRouter
       call (`slop/audio/whisper.py`) — nothing of it ports.
     - **Hangul normalisation + lyric-sheet matching — the named new step:**
       Whisper transcribes freely, so its tokens must be normalised
       (spacing, particle-ending differences) and fuzzy-matched against each
       lyric line to anchor line boundaries. Claudia's `merge.py` rules do
       not cover this — they assume a *forced* aligner that already emits
       sheet-aligned tokens (confidence ≥ 0.5 keeps the forced token, else
       Whisper fills), and its `norm` strips to `[a-z]`, which would erase
       Hangul outright.
     - **Merge/correction rules — the part that ports:** energy-rise
       correction, interpolation of unplaced lines between anchored
       neighbours, low-confidence rows to the ear-check list.
     Method-alike alternative to spike: a Korean-capable CTC *forced*
     aligner (torchaudio's `MMS_FA` bundle) emits per-sheet tokens with
     scores, so claudia's merge rules would apply directly. It is **not** the
     smaller port — its prep is real:
     - **Romanisation step:** `MMS_FA`'s tokenizer takes Latin characters,
       so each Hangul lyric line must first be romanised with uroman (MIT,
       `pip install uroman`), and the aligned romanised tokens mapped back to
       their Hangul line — a step of its own, like the Whisper path's
       fuzzy-match.
     - **Dependency weight:** pulls in `torch` + `torchaudio` (large wheels)
       plus a model download; the default Whisper path can use
       `faster-whisper` (CTranslate2, no torch). torchaudio is in maintenance
       mode since 2.8 — `forced_align` is still documented in 2.11; pin and
       re-verify at implementation time.
     - **Weight licence:** **CC-BY-NC 4.0** (licence table above). Usable
       only if the owner rules the film's use acceptable under
       non-commercial terms; otherwise the spike is a timing reference only
       and Whisper + fuzzy-match is the path.
     The spike runs only if Whisper + fuzzy-match misses the §3.2 pass bar.
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
- **Dependencies:** `faster-whisper` (preferred, no torch) or `whisper`
  (optional extra; tests mock its output); spike-only and not in the default
  extra: `torch` + `torchaudio` + `uroman` (MMS_FA, NC weights); `demucs`
  optional; numpy/scipy/librosa/soundfile already present.
- **Verification:** synthetic-audio unit tests (align on a generated tone track
  with known onsets; avsync on shifted copies — Claudia's own test style);
  real verification = the Korean-song run in §3.2.
- **License/terms:** MIT code paths; whisper/faster-whisper MIT; demucs MIT +
  flagged weights; MMS_FA weights CC-BY-NC 4.0 (spike only, owner decision);
  uroman MIT; wav2vec2/CMUdict not ported.

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
- **Port:** a `revision` object **inside the new take's check record**
  (`qc/conti/<shot>/<take sha16>.json`, written by `check_take`): the user's
  instruction verbatim, the rejected take's `take_sha256` it replaces, the
  persisted conti request that rewrote the field (§1.1), the prompt diff and
  timestamps; `conti_sha256` already tells the two conti versions apart. Not
  a separate file in that folder — `_records` (`conti_brief.py:206-208`)
  would load it as a take and `conti-metrics` would count it as an attempt.
  Consumed by the existing replace-shot path. The ladder (keep synced span →
  cover clip → cut away) becomes an advisory in the check record, not
  automation.
- **Dependencies:** none new.
- **Verification:** unit test on the record (including: take 2 with a
  `revision` object leaves `conti-metrics` at exactly 2 attempts) + the §4
  revision test end-to-end.
- **vs existing Film Unit:** all state lands in existing dirs; no parallel
  candidate DB.

### 1.6 Role split of subscription services / APIs / local execution — **design-reference-only**

- **Claudia code paths:** `docs/ARCHITECTURE.md`, `docs/96-relay.md`,
  `README.md` service table — Claude Code/Opus (director, local files), Grok
  bot (relay + browser executor), user's Midjourney/Suno (manual), OpenRouter/
  Higgsfield (paid API), local FFmpeg/Python.
- **Film Unit's split (kept):** director/reasoning = the user's own Claude
  Code/Gemini CLI/Claude web subscription via persisted request/answer pairs
  and copy-paste (§1.1); image generation = user's browser, their
  subscription app (Gemini
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

### 1.7 Cost control and production failure recovery — ledger **reuse-as-is**; failure checklist **port-with-modification** (doc only)

- **Claudia code paths:** `templates/project/project.json` budget block +
  `confirm_over_usd`, `slop spend` ledger reading provider-reported usage,
  estimate-before-submit, `spend --freeze`, `docs/99-failures.md`.
- **What Film Unit already has — the ledger exists on both sides:**
  `engine/budget.py` (estimate → exact approval → reserve; worst-case cap;
  failed stays reserved) and `engine/segment_gen.py` (SUBMIT_INTENT journal
  before any side effect; UNKNOWN fencing) cover the paid side, and the
  *manual* side is already recorded too — `check_take`
  (`engine/conti_brief.py:257`) stores `attempt`, `service`, `units`
  (subscription units, e.g. Flow credits) and `human_minutes` per take keyed
  to the conti digest, and `conti-metrics` (`conti_brief.py:416`) sums
  attempts/units/minutes per shot and in total. REDESIGN_W00 §2 names
  `qc/conti/` + `docs/evidence/hard-cut-01/` as the numbers' home, and its §5
  roadmap marks this ledger (R2) done ✅.
- **Dropped as duplicate (r1 review):** the earlier-planned
  `subscription_units`/`attempt_no`/`human_minutes` fields on `handoff_log`,
  `engine/manual_spend.py` and `render/manual_spend.json` — a second ledger
  over state `qc/conti/` already records and `conti-metrics` already
  aggregates.
- **Port (what actually remains):** `docs/99-failures.md` as a Film Unit
  failure checklist adapted to manual mode — stale tab, wrong file imported,
  download folder mis-assignment, silent provider decline → a user-visible
  "still NOTHING_IMPORTED" rather than a hung state. Brief-text additions so
  the user knows to enter `units`/`human_minutes` when they run
  `conti-check`.
- **Dependencies:** none.
- **Verification:** the `conti-metrics` rollup is already tested (R2
  acceptance); the §4 report reads it — there is no new ledger to prove. The
  failure checklist is a doc, exercised during the §4 run.
- **Duplicate-charge prevention:** unchanged — content-keyed takes
  (`engine/takes.py`) mean re-importing the same file is idempotent.

### 1.8 Explicitly excluded (whole components)

| Claudia component | Why excluded |
|---|---|
| `slop/relay.py` chat relay (Grok bot inbox/outbox) | Requires the Grok bot + account automation; no driver available in Film Unit |
| `slop/gen/{openrouter,higgsfield}.py`, `slop/fetch.py`, paid adapters | Hard rule: no paid APIs, no keys |
| Grok-as-browser-executor (Midjourney paste loops) | ToS automation bans (§1.0.3, MJ ToS link above); Film Unit handoff policy |
| `slop/taste/` (arena, embedding server, tunnels) | Whole subsystem; out of scope for the hard cut — revisit if taste iteration becomes the bottleneck |
| `runner/deleg.mjs` as shipped (engine spawning, env scrubbing, tgz pack/unpack, `wire`) | Claude Code/Gemini CLI runtime assumptions + supervisor; Film Unit ports only the persisted-request idea (§1.1) |
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
  → Korean lyric-line alignment (new engine/align.py → proposed cues)
  → reviewer confirms cues (existing lyrics review fingerprint)
  → conti/brief for the scene's shots (conti_brief + `look`/`far` canon text + pace rules)
  → manual generation (user's browser: image app for first frame →
     Flow/Veo or Grok Imagine for the clip) via engine/handoff.py briefs
  → import → mechanical checks + sync-measure + vision-judge brief
     (engine/conti_brief.check_take + new engine/sync_check.py)
  → accept take → ONE revision cycle (new §1.5 revision record → new take →
     replace shot)
  → compile Preview (existing compositor: real audio + exact ASS/SRT
     subtitles) → Final after locks
```

**Prerequisite — REDESIGN_W00 R3 (settings-based approvals).** The scene runs
under the `production` block in `project.yaml` (`approver`,
`max_attempts_per_shot`, `services`, `gates: minimal`): with `gates: minimal`
the autopilot reaches PREVIEW_READY on a LEGACY project with no lock
ceremony — the PLAN/WAVE/FINAL ladder, route decision and spend/delivery
approvals run only under `gates: full` (REDESIGN_W00 §4). "Final after locks"
above is the existing production LOCK at `compile-final` — unchanged. The
autopilot is reused as-is *on top of* that setting; R3 is a named dependency
of this path, not something this port re-implements.

### Files to change or reuse (phase 2)

| File | Change |
|---|---|
| `engine/align.py` **(new)** | Korean lyric-line timing → proposed cues + ear-check list; output shaped to `lyrics_timed.json`; local Whisper (new code, optional extra; `faster-whisper` preferred, no torch) + Hangul normalisation/fuzzy-match to the lyric sheet (new step) + `slop/audio/merge.py` correction rules (the part that ports). MMS_FA forced-aligner spike only if that misses the §3.2 bar — needs uroman romanisation + mapping back to Hangul lines, torch/torchaudio, and NC-licensed weights (owner decision before any use) |
| `engine/sync_check.py` **(new)** | Port of `slop/sync/avsync.py` (numpy+FFmpeg only): lag, synced span, divergence, suggested cut; written as fields of the take's existing check record in `qc/conti/<shot>/` — no new file there |
| `engine/audio.py` | Add optional vocal stem (demucs if installed / user-supplied stem / vocal-band proxy); master SHA protection unchanged |
| `engine/packets.py`, `engine/conti_brief.py` | Optional character `far` field (canon text = the existing, also optional `look`/`invariants`; fallback far → look → invariants → warning); fixed clause order; sung-clip vs motion-clip prompt grammar; per-service capability notes in briefs |
| `engine/conti.py`, `engine/conti_brief.py` (requests) | Persist request text (§1.1): `conti/requests/<sha16>.txt`; `qc/conti/<shot>/<take sha16>.judge-<sha16>.txt` (never `*.json` there); optional `request_sha256` added to conti `KEYS` |
| `engine/handoff.py` | Brief gains "which app, expected clip length, what to download" and reminds the user to record `units`/`human_minutes` at `conti-check` — no new ledger fields; `handoff_log` unchanged |
| `project.yaml` schema + `engine/w00_gate.py` | **Dependency (REDESIGN_W00 R3):** `production` block, approver read from settings, `gates: minimal` — required for the scene to reach Preview without lock ceremony |
| `engine/conti_brief.py` (revision) | `check_take` gains an optional `revision` object in the take's own check record — instruction→request→prompt→take chain (§1.5); no new module, no extra file in `qc/conti/<shot>/` |
| `app/handoff_ui.py`, `app/brief_board.py` | Surface the new brief fields and persisted requests |
| `engine/autopilot.py`, `engine/builds.py`, `engine/compositor.py`, `engine/lyrics.py`, `engine/takes.py`, `engine/budget.py` | **Reuse as-is** — gates, immutable builds, exact-timing subtitles, take dedup, caps all stay |
| `tests/` | Fast synthetic tests only: alignment on fixture word timestamps, avsync on shifted WAVs, `conti-metrics` unchanged by persisted requests and revision objects, prompt-clause snapshots (incl. a bible with no `look`). No network, no model downloads, no torch in CI |

**No duplicate engine/state:** every new artifact lands in existing dirs
(`analysis/`, `conti/`, `qc/conti/`, `render/`, `lyrics/`) under existing schemas; no new
job queue, no new takes DB, no second ledger, no second state machine —
autopilot gates and the persisted request/answer records follow the
plain-file convention already in use.

---

## 3. Adaptation plan (owner-task section 3)

### 3.1 Characters and 2D, not Claudia and photoreal

- `look`/`far` identity text and every style word come from the project's own
  `bible/characters.yaml` + style bible. The Claudia canon text is excluded
  (§1.8). A 2D-animated lead is expressed by that identity text + style tail
  + first-frame reference — the same fields photoreal would use, different
  content. Tests will include a 2D-style bible fixture so "photoreal" can
  never be the hardcoded default.

### 3.2 Korean real-song verification plan

Subject: the owner's real Korean-language master (the film's song), supplied
via `input/`. Steps and pass criteria:

1. `analyze` on the master — beat map + tempo; master SHA unchanged (asserted).
2. `align` — Whisper timings over the vocal band → Hangul-matched proposed
   cues + ear-check list. **Pass:** the ear-check list exists; user confirms
   ≥ the sampled line starts; unresolved rows remain flagged, never guessed.
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
| Elapsed wall time | brief/check/build timestamps |
| Real cost | `conti-metrics` totals — attempts, subscription `units`, `human_minutes` from `qc/conti/` records — + $0 marginal cash |
| Failures | every fenced/UNKNOWN/failed import, verbatim |
| Unsupported items | anything still not producible |
| Test-pass counts | pytest tallies — **separate from production success** |

### Risks and mitigations

| Risk | Mitigation |
|---|---|
| Whisper timing drifts on sung Korean (melisma, ad-libs) or fuzzy match misses spacing/particle variants | ear-check list + energy-rise correction + existing manual cue editor; unresolved stays flagged. MMS_FA forced-aligner spike as a further option — costs a uroman romanisation + map-back step and a torch install, and its CC-BY-NC weights need an owner decision before any use in the film |
| Flow doesn't copy the vocal → lip-sync can't be measured as audio-copy | capability-gated: sync-check only runs on audio-carrying takes expected to copy; otherwise vision-judge + cut structure; say "unmeasurable" rather than fake a number |
| Clip length quantization (4/6/8 s) vs shot length | `conti_brief.clip_length` snapping already exists; brief prints the mismatch |
| Flow monthly credit ceiling (Pro: 1,000/mo; ~20/gen Fast) | `conti-metrics` units ledger + worst-case estimate before the user starts; scene chosen to fit in a handful of generations |
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

---

## 5. Phase-2 build report (2026-10-15)

### 5.1 What was built (all under existing dirs and schemas)

| Area (§1) | Built | Where |
|---|---|---|
| 1.4 Korean lyric-line timing | `lyrics-align`: forced alignment of the **lyric sheet** on the vocal stem with local faster-whisper (cross-attention DTW, float32), claudia energy-rise + interpolation rules, duration-outlier repair, repeat-offset and low-confidence ear check. Writes `lyrics/align_proposal.json` (importable by `lyrics-import`, review empty) and `lyrics/align_report.json`; never `lyrics_timed.json` | `engine/align.py` |
| 1.4 vocal stem | `ensure_vocals`: `input/vocals.<wav\|mp3>` (supplied) → demucs if importable → harmonic/vocal-band proxy labelled `proxy`; cached on the master sha | `engine/audio.py` |
| 1.4 sync measurement | `conti-sync`: claudia `avsync.py` port (lag, NCC, synced spans, divergence, beat-snapped cut suggestion) into the take's check record; a take with no audio stream is recorded `unmeasurable`, never lag 0 | `engine/sync_check.py` |
| 1.1 persisted requests | `conti-request` → `conti/requests/<sha16>.txt`; `conti-submit --request <id>` stores `request_sha256` (one optional key; excluded from the conti digest); `conti-judge-request` → `qc/conti/<shot>/<take sha16>.judge-<sha16>.txt` (not `*.json`, so never counted as a take); director world/shots requests appended to `bible/director_requests.jsonl` | `engine/conti.py`, `engine/conti_brief.py`, `engine/handoff.py` |
| 1.5 revision chain | `conti-revision`: the verbatim instruction is stored **inside the revised take's check record** (`revision_request`, with the conti and prompts as they stood). The person changes the conti field; the next `conti-check` writes `revision` into the new take's record: instruction, revised take, `conti_changed` field paths, prompt diff — and marks the request fulfilled. No new record file | `engine/conti_brief.py` |
| 1.2 identity text | optional `far` beside `look`; wide framings use `far`, otherwise `look`/`invariants` as before; sung vs motion video prompt (sung shots quote the cue words and close on "only the lead sings") | `engine/conti_brief.py`, `engine/director.py` |
| R3 dependency | `project.yaml` `production` block (`mode`, `approver`, `max_attempts_per_shot: 3`, `services`, `gates: minimal`); `gates: minimal` lets the **manual** path reach PREVIEW_READY without the lock ceremony (paid/mock renderers keep it); `w00_gate` reads the approver from `production.approver`, then the user setting, then the built-in name; `conti-check` warns past the attempt cap | `engine/core.py`, `engine/autopilot.py`, `engine/w00_gate.py` |
| 1.7 ledger | reused as-is: `conti-check --service --units --human-minutes`, `conti-metrics` (now also counts revisions) | — |

No new job queue, takes DB, ledger or state machine was added.

### 5.2 Where the build deviated from the reviewed plan, and why

1. **Korean alignment is sheet-forced, not "Whisper transcription + fuzzy match".**
   On the real song, free transcription of the separated vocal (faster-whisper
   small/medium, VAD on or off, prompted with the sheet or not) dropped whole
   verses, looped on hallucinations ("ㅋㅋㅋ…", "I'll be back…") and, matched
   to the sheet, anchored the first chorus to the third. Forcing the known
   sheet text through Whisper's own alignment heads and accepting a line only
   where it is the first thing sung in the window was stable — and needs no
   MMS_FA (CC-BY-NC weights), no uroman, no torch. `norm_text` stays (Hangul-safe)
   for same-text grouping. The MMS_FA spike was therefore not run.
2. **int8 is unsafe on the run machine.** CTranslate2 int8 returned different
   encoder outputs for identical input (max abs diff 36–78 between two calls);
   float32 was bit-identical. `compute_type=float32` is the default
   (`lyrics-align --compute-type`). Part of the "Whisper can't hear sung Korean"
   impression earlier in the run was this bug.
3. **Whisper needs the speech gate off for singing.** Default
   `no_speech_threshold` discarded every sung segment; the forced path does not
   use it.
4. **Revision record lives inside the take check records** (plan revision 3),
   carrying `conti_changed` so a revision is expressed as a conti field change
   (REDESIGN_W00 §2.2) rather than a free-text side record.

### 5.3 The real run (Korean song, end to end inside Film Unit)

**Input — real Korean song, not the film master.** The owner's master is not
on this machine (no audio anywhere in the workspace; OneDrive not mounted
here). The real Korean song used instead: *2021 아리랑 응원가* sung by 윤도현
(Wikimedia Commons `File:2021 아리랑 응원가 윤도현 가창 음원.flac`, CC BY 4.0,
source Korea Copyright Commission 공유마당; 201.2 s, flac sha256
`498a0e76…3bba`), converted losslessly to WAV for `init`. Attribution
required: 윤도현, 성윤용, 김민기, 강규용, 김승남, 박상우, 안도형, 윤정훈 / Korea
Copyright Commission, CC BY 4.0. It is a modern band arrangement with rap
sections — a hard case for sung-Korean alignment.

**Lyric sheet.** The traditional Arirang verse (public domain) as one
`[아리랑]` section plus two `[아리랑]` repeat markers (12 effective lines).
The arrangement's rap/bridge lines have no published sheet (the 공유마당 page
was unreachable from this box; a web search found none), so they are **not**
on the sheet; the aligner reports those spans as long gaps. The verse order
was chosen by the operator from what is sung — that choice is part of "the
user supplies lyrics", and it is the owner's to correct.

**Vocal stem.** demucs 4.0.1 `htdemucs --two-stems vocals` (CPU, separate venv,
weights licence flagged in §0) supplied as `input/vocals.wav` → source
`supplied`.

**Alignment result** (`docs/evidence/claudia-hardcut-01/align_report.json`):
12/12 lines placed (10 forced, 2 forced+repair, 0 interpolated, 0 unplaced) in
71 s of CPU wall time; 50 forced alignments over 33 vocal onsets.

| Line | Chorus 1 | Chorus 2 | Chorus 3 |
|---|---|---|---|
| 아리랑 아리랑 아라리요 | 16.60–22.76 | 75.69–81.87 | 136.59–142.73 (repaired) |
| 아리랑 고개로 넘어간다 | 23.13–30.07 | 82.24–89.30 | 143.15–150.31 (repaired) |
| 나를 버리고 가시는 님은 | 31.30–38.02 | 90.39–97.03 | 150.69–157.93 |
| 십리도 못 가서 발병난다 | 38.02–45.31 | 97.03–103.94 | 157.93–165.42 |

Internal consistency (not ground truth): chorus 2 and 3 line starts sit
59.1–59.9 s and 119.4–120.0 s after chorus 1's; the ear-check list holds 3
rows (one confidence 0.45, one repair note, one −0.60 s repeat offset).
**Pass bar (§3.2 step 2) — not yet judged.** This run has **no human ear
check**: the pass criterion ("≥ 80 % of sampled line starts within ±300 ms of
the reviewer's ear-marked start") needs the owner to listen; the cues were
imported **unreviewed** and Preview says so.

**Scene and revision** (`docs/evidence/claudia-hardcut-01/run.json`): the hard
cut is shot `S027` = 30.80–38.80 s (8.0 s → Flow's 8 s clip), the line
"나를 버리고 가시는 님은" (cue 31.30–38.02 s), one character (the user's own
2D lead "Haneul", `look` + `far` in the bible — no Claudia canon, style
`2D cel animation`, rule `no photorealism`). Path, all through Film Unit
commands/functions:

1. `make_package` → `split-shot` at 30 800 / 38 800 ms (cut edits never move cues).
2. `lyrics-import` of the proposal **without a reviewer**.
3. `conti-request` persisted as `conti/requests/06449c30be9be465.txt`; the conti
   answer (written by Claude Opus 5.5 in this Claude Code session — the
   "director = user's subscription LLM" role, not the owner) stored with
   `conti-submit --request 06449c30be9be465`. 4 acceptance criteria. Brief:
   sung shot, 8 s clip.
4. **Take 1** imported → `conti-check`: DURATION / ASPECT / RESOLUTION / MOTION
   all PASS; `conti-sync` → `unmeasurable` (the take has no audio stream —
   correct, not a fake lag 0); judge request persisted. `compile-preview` →
   **B0001**.
5. **Revision instruction** (verbatim): *"손은 첫 박에 들지 말고 '가시는'에서
   고개 쪽으로 드세요"* → `conti-revision` on take 1 → one conti field changed
   (`blocking[0].motion_beats[0]` at_ms + action: gesture moved from clip
   0.60 s to 3.84 s = the aligned word "가시는" at 34.64 s) → brief regenerated
   → **take 2** → `conti-check` records `revision` (`conti_changed` =
   `blocking[0].motion_beats[0].action`, `…at_ms`; prompt diff) and marks
   take 1's request fulfilled → `compile-preview` → **B0002**.
6. Vision judge on take 2 (Claude, from the 5 sampled frames, via
   `conti-judge-submit`): criteria 1, 2, 4 PASS; 3 (push-in) UNSURE → stays
   PENDING for a person. Overall verdict PENDING — correct.
7. Invariants after B0002 (asserted by the run script): master sha unchanged
   ✅, lyric timing fingerprint unchanged ✅, every other shot's manifest entry
   byte-identical ✅, every B0001 file's sha unchanged ✅.

### 5.4 Before / after outputs

| | Before (B0001, take 1) | After (B0002, take 2) |
|---|---|---|
| Take sha256 | `18ea8dec7939ddfd…` | `47b4ebce7c659eb9…` |
| Gesture | hand up from clip 0.6 s | hand rises on "가시는" (clip 3.84 s) |
| Scene clip (8 s, from MASTER_SUBBED, real audio + Korean subtitle) | `docs/evidence/claudia-hardcut-01/scene_before.mp4` | `…/scene_after.mp4` |
| Stills at 1.0/2.5/4.0/6.0 s | `before_*.jpg` | `after_*.jpg` (grid: `before_after_grid.jpg`) |
| Check record | `check_18ea8dec7939ddfd.json` | `check_47b4ebce7c659eb9.json` (with `revision`, judgements) |

The run is reproducible from `docs/evidence/claudia-hardcut-01/stage1.sh`
(master → `lyrics-align`) and `stage2.py` (package → B0002 → invariants);
`render_local_take.py` is the local stand-in renderer — evidence tooling, not
an engine feature. The full-length builds (`MASTER_CLEAN.mp4`, `MASTER_SUBBED.mp4`, `lyrics.ass/.srt`)
are in the run project (`projects/arirang_hardcut/builds/B0001`, `B0002`, not
committed — `projects/` is git-ignored); only the 8 s scene excerpts are
committed. The other 30-odd shots of the song are timing slates — this was a
one-shot scene test.

### 5.5 Human intervention, time, cost, failures, unsupported

- **Human intervention: 0 owner actions.** Every step — including the parts
  that are the owner's in production (choosing the lyric verse order,
  answering the conti, issuing the revision instruction, the judge) — was
  performed by the agent in this session and is labelled so. The owner's ear
  check of the cues and acceptance of the scene are still outstanding.
- **Elapsed (monotonic CPU wall, shared machine under load 6–130):**
  vocal separation 23 min (demucs, CPU, contended); `lyrics-align` 71 s;
  package → B0001 → revision → B0002 incl. both local renders (12.6 s, 9.9 s)
  and both full-song Preview compiles: 71 s. Investigation/tuning of the
  aligner took most of the session (~1 h, see §5.2).
- **Real cost: $0 cash, 0 subscription units.** No paid API, no API key, no
  purchase. Tools: FFmpeg, faster-whisper (MIT, model from huggingface.co),
  demucs (MIT code; htdemucs weights licence unflagged), all local; the agent
  itself ran on the Claude Code subscription. `conti-metrics`: 2 attempts,
  0 units, 0 human minutes, 1 revision.
- **Not a generation — the takes are `LOCAL_PROCEDURAL`.** No image/video app
  (Gemini, Flow, Grok Imagine) is reachable from this machine, and Film Unit
  never scripts consumer web apps. Both takes were drawn by a local PIL +
  FFmpeg script in the bible's ink-on-paper style (mouth driven by the real
  vocal stem, gesture/turn/push from the conti beats) and imported through the
  normal `import-asset` path. They prove the pipeline and the revision loop,
  **not** look quality: they are crude placeholders, not usable as final film
  footage. The first real Flow/Gemini take remains the owner's step
  (REDESIGN_W00 R5).
- **Failures on the way (verbatim causes):** Whisper free transcription on the
  sung stem: verses missing / hallucination loops; CTranslate2 int8 encoder
  non-determinism on this CPU; faster-whisper's PyAV decoder failed
  (`open() got an unexpected keyword argument 'metadata_errors'`) — the engine
  decodes with FFmpeg instead; large-v3 on the full mix was OOM-killed;
  `no_speech_threshold` discarded all singing; a concurrent Devin builder
  wrote in the same worktree for the first ~2 h (single-writer rule — this
  session waited, then took over its uncommitted work and reworked it).
- **Unsupported / not done:** the owner's own film master (not on this
  machine); owner ear-check of cues (pass bar §3.2 not yet judged); real
  subscription image/video generation; lip-sync *measurement* of a real take
  (only capability-gated `unmeasurable` was exercised; the copy-mode
  measurement is covered by synthetic tests only); rap/bridge lyrics without a
  published sheet; Final (`compile-final`) — needs reviewed lyrics, LOCK and
  real takes; MMS_FA spike (not needed, §5.2); service prices in §3.3 were
  verified at plan time (2026-10-14) and **not** re-fetched in this build.

### 5.6 Test-pass counts (separate from production success)

See §5.7 — the pytest tally is recorded there and in the PR body. A green
suite proves the code paths on synthetic fixtures; production success is only
what §5.3–5.5 show.

### 5.7 Test-pass counts

- `tests/test_claudia_port.py`: **19 passed** (synthetic fixtures; a fake
  forced aligner — no model, no network).
- Affected suites together (`test_claudia_port`, `test_conti`,
  `test_conti_brief`, `test_handoff`, `test_autopilot`, `test_director`,
  `test_packets`, `test_lyrics`, `test_anim_022`, `test_ui_handoff`,
  `test_schema_presets`): **150 passed, 1 failed** in 221 s. The failure,
  `test_autopilot.py::test_gemini_video_end_to_end_with_the_chosen_image_model`,
  fails identically on unmodified `origin/main` on this machine (local FFmpeg
  — a known box-specific failure); CI is the authority for it.
- The full suite was started but not finished locally (≈1 h on this loaded
  machine); CI runs it on the PR head.

These counts are code-path evidence only. **Production success in this PR is
limited to what §5.3–5.5 show:** a real Korean song aligned line-by-line
(unreviewed), one scene composed with real audio and Korean subtitles, one
revision applied end to end — with locally drawn placeholder takes, not real
generations.

### 5.8 Owner decisions this run surfaces

1. Ear-check the 12 proposed cues (start with the 3 ear-check rows), then
   `lyrics-import --reviewer` — or correct them in the editor.
2. Supply the film's own master + lyric sheet (and any rap/bridge text) and
   run `lyrics-align` on it.
3. Make the first real take of `S027`-style scenes in Flow / Gemini from the
   persisted brief and import it in place of the `LOCAL_PROCEDURAL` take.
4. Whether demucs (torch, unflagged htdemucs weights) may run on the owner's
   machine, or stems come from the owner's DAW (`input/vocals.wav`).
