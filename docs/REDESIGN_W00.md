# REDESIGN W00 — one hard real cut first

Design audit for `film-unit-mv-studio` at main `c4c54b5` (PRs #98, #101, #103),
revised by the lead designer on 2026-10-14; it supersedes the earlier draft on
this branch. Owner constraints: **(A)** cost only inside subscriptions JunTae
already pays for — no API keys, no pay-per-use, no extra credits; **(B)** high
output quality; **(C)** a maximally detailed human+LLM conti stage; **(D)** the
smallest possible human load in production.

## 1. Verdict on the consultant's points

**Agree — the path that produces good shots is unproven.** The repo says so itself:

- `README.md:7` — zero real external AI video generations on record; no
  finished real-song MV verified.
- `engine/segment_gen.py:45-49` — the only wired segment adapters are
  `fake_segment` (FAKE/UNQUALIFIED) and `rife_onnx`; `gemini_video` is a
  start-only declaration whose submit path refuses.
- `docs/evidence/anim-015-rife/demo.json` — the RIFE demo recorded `ran:false`,
  `output_sha256:null`, `weights_state:MISSING`.
- `engine/drive_oauth.py:6,72` — `GoogleOAuthFlow` is a disabled stub; the
  desktop archive panel only ever connects through `FakeOAuthFlow`
  (`app/archive_ui.py:85,143`). The real loopback flow sits in unmerged PR #98,
  whose body records WAITING / UNQUALIFIED / PENDING / NOT_AUTHORIZED and
  `real_route_completed=false`.
- `docs/CHEAP_ANIMATION_TEST.md:3` — the 5-second motion comparison prepared
  on 2026-09-11 was never run.

**Agree — the approval/proof system outweighs the product.** `engine/w00_gate.py`
(1,140 lines) hardcodes `PRIMARY_APPROVER = "박준태"` (line 68) and refuses any
other HUMAN approval without a delegation record (lines 226-230). Around it sit
`animation_locks.py` (PLAN/WAVE/FINAL locks, KEEP/CHANGE/MIX route decision),
`animation_review.py`, `capability_registry.py`, `render_provenance.py`, and the
autopilot ladder NEEDS_PLAN_LOCK → NEEDS_ROUTE_DECISION → NEEDS_WAVE_LOCK →
NEEDS_CUT_REVIEW → NEEDS_FINAL_LOCK (`engine/autopilot.py:183-249`). None of it
has yet guarded a real shot.

**Agree with one nuance — "dev done" is not "usable".** Mock-only adapters are
real. The FFmpeg drift that grew its own adjudication trail (#95/#96 → #99/#101,
`docs/evidence/anim-015/encoding-decision.json`) is process spent before product.
But the *failure semantics* — UNKNOWN fencing, never resubmit, one job identity
per input — are exactly right the moment a metered service is attached. Keep
them as code defaults; stop making the human perform them as ceremony.

**Disagree on one framing.** PR #98 is not a bad PR; it is a correct answer to a
problem the product does not have yet (remote 1080p compose over Drive). The
fault is ordering. Freeze it, do not rework it.

**Keep, as the consultant says:** lyric timing independent of cut edits
(`engine/lyrics.py:135` `timing_fingerprint` hashes cues only; `timeline.py`
edits never move cues); per-shot replacement with immutable builds
(`engine/builds.py`, `README.md:105-127`); image sequences, generated clips and
layer compositing (`engine/animation_assets.py`, `compositor.py`); re-review of
only the changed range (`README.md:68-77`).

## 2. Revised product goal and definition of done

**Goal.** Make one hard real cut of the main piece — 5–10 s, the real song, the
real character — and finish one revision of it, inside FILM UNIT, on
subscription services only. Then repeat shot by shot.

**The hard cut is done when, with real files on disk:**

1. **Look and motion** — JunTae accepts the take against the conti's acceptance
   criteria: every criterion PASS, not "looks nice".
2. **A targeted revision works** — one conti field changes (the hand gesture in
   `blocking[].motion_beats`), the brief regenerates, take 2 is made and checked;
   every other shot, the lyric timing and earlier builds stay byte-identical.
3. **The numbers exist** — attempts, subscription units, wall time and human
   minutes per take, recorded in `qc/conti/` and summarized in
   `docs/evidence/hard-cut-01/`.
4. **It is in the film** — the take compiles into a Preview with the real audio
   and the reviewed subtitles.

**The product is done** when that loop, repeated over the song, ends in a
`compile-final` the owner approves. Nothing else is a milestone.

## 3. Pipeline — subscription-only at every step

Cost rule: each stage names the subscription and how it is driven. At the
subscription limit the stage **waits** — the outstanding packets stay listed and
nothing is submitted anywhere; there is no paid fallback because no API key
exists on the machine. Automation rule: FILM UNIT drives only official
interfaces — a CLI the vendor ships with the subscription, or copy/paste/drop by
the human. Consumer web apps are never scripted (`engine/handoff.py:3-6`).

| Stage | Subscription and how it is driven | Automatic | Human touch | At the limit |
|---|---|---|---|---|
| **Conti** | Claude (claude.ai or Claude Code CLI), ChatGPT / Codex CLI, Gemini app / Gemini CLI | `conti-request` builds the request from manifest, timed cues, beats, bible, neighbours and existing conti; `conti-submit` validates and stores; `conti-status` lists `missing` and `open_questions` | Discuss in as many turns as needed; settle open questions; repeat until every shot is READY | Session cap → wait; a new conversation is fine, the request is self-contained |
| **Character / asset lock** | Gemini app (Nano Banana), ChatGPT images | Reference-sheet and first-frame prompts from bible + conti (`conti-brief`); import validation, aspect warning, hand-off log | Pick the look once; approve each first frame (it carries most of the final quality) | Wait |
| **Shot generation** | Flow (Veo 3.1) under Google AI Pro/Ultra, or Grok Imagine (SuperGrok) | Motion-only prompt from the conti with every beat in clip-relative seconds, clip length snapped to 4/6/8 s, attachments listed; batched packets per sitting | Paste, generate, download, drop the file — the one step that stays manual | Monthly credits exhausted → packets wait for the reset |
| **QA vs conti** | Local FFmpeg; a vision-capable LLM through its subscription CLI (Claude Code, Gemini CLI read local images) or chat with attached frames | On import: duration / aspect / resolution / stillness checks, five-frame contact sheet, judge request with the conti's criteria; the answer becomes per-criterion PASS/FAIL (`conti-check`, `conti-judge-*`) | None unless a criterion FAILs or the judge is unsure; then one screen per shot | Wait |
| **Targeted revision** | Same as generation | Change one conti field → brief regenerates → next take is attempt n+1 against the new conti digest; the loop stops at the per-shot attempt cap (project setting, default 3) | Confirm the failed criterion, or stop | Wait |
| **Assembly / export** | Local FFmpeg | `compile-preview` / `compile-final`, font and cmap checks, build history | Watch the Preview; approve the Final | — |

Human touch points in production: **(1)** conti decisions, **(2)** look and
first-frame approval, **(3)** one hand-off sitting per batch, **(4)** confirming a
FAIL or UNSURE criterion, **(5)** Final approval. Everything else is a record
FILM UNIT writes itself.

Honest limit of (D): as long as (A) holds and consumer apps are not scripted,
generation itself cannot be automatic — a person performs the paste and the
download. The design makes that step batched and fast; removing it is a
decision in §6, not something to build quietly.

The conti stage in detail (C): the discussion is open-ended — the request text
carries everything (brief, style, per-shot manifest fields, timed lyric cues and
measured beats inside the shot, cast and location entries, the neighbours'
direction, any existing conti) so the human and the model can argue framing,
pose, timing and transitions for as long as they like. The model's final answer
is one JSON object per shot; FILM UNIT rejects anything outside the shot's time
range, outside the declared cast, or escaping the project, and shows exactly
which fields keep a shot unsettled. `acceptance_criteria` must be checkable
statements, because they become the QA checklist and the judge's questions.
`open_questions` non-empty means "not settled"; READY is the acceptance state —
no lock record is added.

## 4. Keep, simplify, freeze, delete

**Keep (critical path).** `compiler.py`, `builds.py`, replay; the lyrics
pipeline; `timeline.py` edits; `handoff.py` + `packets.py` (already the
subscription-only path); `conti.py` and `conti_brief.py`; `shot_board`,
`review_diff`, `quality_diagnostics` (mechanical checks); the FFmpeg encode
path (owner waiver ANIM015-ENC-20261008).

**Simplify — approvals become settings.**

- `project.yaml` gains a `production` block: `approver: <name>`,
  `max_attempts_per_shot: 3`, `services: {text, image, video}`,
  `gates: minimal | full` (default `minimal`). The `PRIMARY_APPROVER` hardcode
  and the delegation registry in `w00_gate.py` read the approver from this
  setting; existing records stay readable.
- Per-shot accept = a conti QA record with verdict PASS and a reviewer name.
  Whole-film approve = the existing production LOCK + reviewer at
  `compile-final`. The PLAN/WAVE/FINAL ladder, route decision, spend approvals
  and delivery approvals leave the critical path and run only under
  `gates: full`.
- UNKNOWN fencing and no-resubmit stay as adapter defaults; no human record is
  required to proceed.

**Freeze.** PR #98 (anim-018): leave open, labelled "frozen until hard-cut
evidence"; no further rebases or review rounds. anim-016's subscription
code-runtime/notebook path, anim-017 scheduler expansions, encoder drivers
beyond FFmpeg, and Drive archives beyond local.

**Delete from the default product (code stays as legacy).** The paid routes
`gemini`, `fal`, `openart`, `economy`, `benchmark` disappear from the model
picker unless `FILM_UNIT_LEGACY_PAID=1`; `fake_segment` remains a test double
and is never shown as a provider.

**Program nodes.** anim-022 (W00 route judgement) and anim-023 (23-node
closeout) close as "superseded by REDESIGN W00; evidence = hard-cut-01" instead
of running their remaining ceremony. `film-studio-qualification` is frozen
until one real MV exists. `PENDING_NODES.json` stays non-executable. No new
program nodes until the R5 evidence lands.

## 5. Re-ordered roadmap (acceptance on real outputs)

- **R1 — detailed conti documents** (done on this branch: `engine/conti.py`,
  `conti-draft/-request/-submit/-status`). Acceptance: validation rejects
  out-of-range timing, unknown cast and path escapes; shots, lyrics, reviews
  and LOCK untouched. ✅
- **R2 — conti → shot brief → take check-in** (this PR: `engine/conti_brief.py`).
  `conti-brief` renders the first-frame prompt and a motion-only video prompt
  with beats in clip-relative seconds and the clip length snapped to 4/6/8 s;
  `conti-check` records duration / aspect / resolution / stillness, a
  five-frame contact sheet, the conti's criteria as PENDING, and the
  attempt/units/minutes ledger; `conti-judge-request` / `conti-judge-submit`
  carry the criteria to a vision LLM and parse its verdicts; `conti-verdict`
  is the human override; `conti-metrics` aggregates. The hand-off video brief
  uses the conti, and a dropped clip is checked automatically. Acceptance: a
  clip shorter than its shot FAILs DURATION; a still clip on a motion shot
  FAILs MOTION; criteria stay PENDING until judged; metrics add up. ✅
- **R3 — settings-based approvals.** The `production` block; approver from
  settings; `gates: minimal` default so the autopilot reaches PREVIEW_READY on
  a LEGACY project with no lock command. Acceptance: existing tests pass with
  the setting; `w00_gate` reads the approver from settings. ✅ (code; the
  claudia port landed the block and the manual-path gate, this slice finished
  it: the approver source order and `production` command, delegations that
  survive an approver change, and `gates` governing the FRAME_ANIMATION_V1
  ladder, route decision, spend and delivery approvals — `animation-init`
  pins `full`, an unset `FRAME_ANIMATION_V1` project is `full`.)
- **R4 — pick the hard cut, finish its conti on the real song.** JunTae and the
  LLM, through `conti-request`. Acceptance: `conti-status` READY for the chosen
  shots on the real project; request and answer kept under
  `docs/evidence/hard-cut-01/`.
- **R5 — make the hard cut.** First frame in the Gemini app, clip in Flow,
  import, auto-check, judge, accept; one targeted revision; `compile-preview`
  with the real audio and subtitles. Acceptance: `conti-metrics` shows real
  attempts, units and minutes; the evidence folder holds both takes' check
  records and the Preview build id.
- **R6 — revision loop.** Failed criterion → LLM rewrites the prompt → new brief
  → attempt n+1, capped by the setting. Acceptance: on the hard cut, one
  revision round completes with no human action other than the hand-off.
  Code: `conti-rewrite-request`, `conti-loop`, the attempt cap in
  `conti-revision`, the conti-change flag on verdicts, and the subscription-CLI
  driver `engine/conti_cli_driver.py` (`production.rewrite_driver: manual|claude_cli`,
  default `manual`): with `claude_cli`, `conti-loop` answers the judge request and
  the rewrite request itself through `claude -p` on the user's Claude login.
  - *Proven by tests with a fake `claude` executable on PATH*
    (`tests/test_conti_cli_driver.py` — fake-CLI tests, not a model): no
    `ANTHROPIC_*` / `CLAUDE_CODE_USE_*` variable reaches the CLI; anything but a
    claude.ai subscription login (`claude auth status`) is refused before a call;
    a monotonic-clock timeout kills the process group and is not retried; the raw
    output, extracted answer and model are written to `qc/conti/<shot>/driver/`
    before validation; a bad answer (not JSON, other shot, unchanged conti,
    out-of-range time, unknown field, silently weakened criteria, wrong take)
    stores nothing; a missing CLI, refusal, timeout, error envelope or rejected
    answer falls back to the paste-it-yourself path with the persisted request.
  - *Proven by one real run on synthetic frames*
    (`docs/evidence/redesign-r6-cli-smoke/`): the flags and the JSON envelope work
    against the real CLI 2.1.290 on a subscription login; the judge read the
    sampled frames and FAILed a test card; the rewrite was stored once the request
    carried `schema_version` and the length limits (two real rejections found
    both). Test pattern only — nothing about a real shot.
  - *Not met:* the acceptance on the hard cut. No real take has been revised, so
    whether the judge is calibrated and whether a rewrite improves the next take
    is unknown, and the generation/download step is still done by a person.
- **R7 — scale.** Packets batched per sitting, downloads auto-assigned, a
  Preview per batch. Acceptance: the first full verse in Preview, with metrics.
  Code: `engine/conti_batch.py`, `conti-batch plan|open|assign|preview|close|status`.
  Sittings hold `production.sitting_limit` shots (default 4) that need a take
  (conti READY, first frame imported, at most 8 s); `open` writes
  `qc/batches/SIT001.json` and a prompt sheet built by the same
  `handoff.video_brief`; `assign` matches a downloads folder's videos to slots by
  shot id in the file name (or a `--map` file) and imports them through the
  ordinary `handoff.import_many` (validation, log, auto conti-check), refusing the
  whole assignment, with nothing imported, on a name carrying two shots, two
  different files for one slot, or a map that contradicts the name or points
  outside the sitting; `preview` runs the ordinary `compile_preview` and records
  attempts, units, human minutes and, per take, whether that exact sha256 is the
  source in the Preview build.
  - *Proven by tests* (`tests/test_conti_batch.py`, ffmpeg test patterns as the
    downloads, the real compiler for the Preview): grouping and the reasons a shot
    is left out, the matching and each refusal, idempotent re-assignment, the
    metrics arithmetic, the take-in-Preview check, and the CLI round trip.
  - *Not met:* the acceptance (the first full verse in Preview) needs real takes.
    Units and minutes are numbers the person types (`--units`, `--human-minutes`,
    or per take at `conti-check`); nothing measures them. A Preview is a
    whole-film build, so each sitting's Preview also contains earlier sittings'
    takes. Generic download names (`Flow_8f3a.mp4`) need a `--map` or a rename;
    nothing guesses from content.
- **R8 — Final.** `compile-final` and the delivery bundle of the real song;
  only then reopen the qualification scope.

## 6. Risks and decisions for JunTae

Decisions. Items 2–5 were taken on 2026-10-15 by the owner's delegate and are
recorded here as decided; items 1 and 6 are still JunTae's.

1. **Primary video app for the hard cut** — Flow (Veo 3.1: 16:9 / 9:16, 4·6·8 s
   clips) vs Grok Imagine vs the Gemini app. Recommended: Flow. **Open**; needed
   before R4.
2. **Hand-off automation** — **decided: keep the strict no-scripting rule for
   consumer video apps** (terms-of-service and account risk). Paste, generate,
   download and drop stay with the person; `handoff.py` is unchanged. A vendor's
   own CLI used on the user's own subscription (item 3) is not a consumer app and
   is not covered by this rule.
3. **Judge and rewrite runtime** — **decided: Claude Code CLI on the user's
   subscription login (`claude -p`)**, never an API key and never a paid API.
   Implemented as `engine/conti_cli_driver.py` behind
   `production.rewrite_driver: manual|claude_cli` (default `manual`); the
   fallback is always the paste-it-yourself path. Gemini CLI stays an alternative that is not built.
4. **PR #98** — **decided: frozen** (left open, no rebases, no review rounds)
   until hard-cut evidence exists.
5. **Program closeout** — **decided: anim-022, anim-023 and
   film-studio-qualification are superseded** by this redesign; they are not
   executed.
6. **Which song and which 5–10 s scene** is the hard cut. **Open.**

Risks: subscription allowances are unmeasured — record them in the hard-cut
evidence rather than guess; character consistency rests on hand-made reference
sheets plus approved first frames (quality risk B, mitigated by first-frame
approval before any clip); an LLM judge can be lenient — criteria must be
checkable statements, and the human confirms only FAIL and UNSURE; Veo's 8 s
cap means shots longer than that are split before generation, which the brief
now warns about.
