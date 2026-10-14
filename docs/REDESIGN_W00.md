# REDESIGN W00 — make one hard real cut first

Design audit revision for `film-unit-mv-studio` at main `c4c54b5`, responding to
the external consultant's review of the codebase, PR #98, PR #101 and PR #103.
Owner constraints applied: subscription-only cost (no API keys, no pay-per-use,
no credits), high output quality, a maximally detailed human+LLM conti stage,
and minimum human load during production.

## 1. Verdict on the consultant's points

**Agree: the path that produces good shots is unproven.** The repo's own records
say so, and harder than the consultant stated:

- `README.md:7` — "실제 외부 AI 영상 생성은 이 저장소의 검증 기록상 **0건**";
  no completed real-song MV is verified.
- `engine/segment_gen.py:48-49` — `gemini_video` is "a start-only capability
  declaration whose submit path refuses."
- `docs/evidence/anim-015-rife/demo.json` — the RIFE demo recorded `ran:false`,
  `output_sha256:null`, `weights_state:MISSING`.
- `engine/remote_qualify.py` does not exist on main; the real remote route is
  unmerged PR #98, whose own review records the in-repo state as
  WAITING/UNQUALIFIED/PENDING/NOT_AUTHORIZED.
- Browser Google login on main runs only through `FakeOAuthFlow`
  (`app/archive_ui.py:143`); the real loopback OAuth ships inside #98.

**Agree: the approval/proof system outweighs a tool that has not finished one
shot.** `engine/w00_gate.py` alone is ~1140 lines, plus `animation_locks.py`,
`animation_review.py`, `capability_registry.py`, `render_provenance.py` and the
journal/durable layer. The approver is hardcoded as "박준태 or an unexpired
recorded delegate" (`engine/w00_gate.py:418`). This machinery protects state no
real production has yet exercised.

**Agree, with nuance, on "dev done vs actually usable."** Adapters merged on
mocks only is real (`engine/segment_fake.py` and `rife_onnx` are the only wired
adapters, `segment_gen.py:45-47`). The FFmpeg adjudication trail
(`docs/evidence/anim-015/encoding-decision.json`, issues #95/#96, PRs #99/#101)
is process weight spent before a product exists. But the failure-classification
rigor (UNKNOWN fencing, never-resubmit, idempotent jobs) is not waste — it is
the one property worth keeping when real money-adjacent services are attached.
It should be demoted from ceremony to settings, not deleted.

**Keep, as the consultant said:** lyric timing is independent of cut edits
(`README.md:66`, `engine/lyrics.py:135-139` — the timing fingerprint excludes
shot data); per-shot replacement with immutable build history
(`README.md:105-127`); imported image sequences, generated clips and layer
compositing (`engine/animation_assets.py`); re-review of only the changed range
(`README.md:68-77`, `engine/animation_review.py`).

**Partial disagreement:** "remote integration still open" is not a defect of
#98's code — its reviews show sound fencing; it is simply ahead of the product.
The fix is ordering, not more review.

## 2. Revised product goal and definition of done

**Goal: make one hard real cut of the main piece and finish a revision of it.**
The W00 principle — build the hardest cut first to choose the production path —
moves from a gate record to the product's actual next milestone.

**Done for the hard cut (a 5–10 s scene from the real song):**

1. Character look and motion reach the level JunTae declares acceptable — he
   picks the reference frame and accepts the take.
2. A targeted edit such as "change only the hand gesture" produces a second
   take of the same shot without touching neighbours or lyrics.
3. Generation cost in subscription units, wall time, retry count and
   human-touch minutes are recorded in `docs/evidence/`.
4. The take is imported into FILM UNIT, compiled into a Preview with the real
   music and reviewed subtitles.

**Done for the product:** the same loop, repeated over the remaining shots of
the real song, ends in a compile-final the owner approves.

## 3. Pipeline (subscription-only at every step)

| Stage | Tools/services (subscription only) | Automation | Human touch |
|---|---|---|---|
| Conti | Claude / ChatGPT / Gemini app via copy-paste (`engine/handoff.py:47-55` pattern) | FILM UNIT builds the request from manifest + timed lyrics + measured beats; validates the returned JSON | Discuss and edit until each shot's conti is READY |
| Character/asset lock | Nano Banana / chosen image app; existing `handoff.import_reference` / `import_frame` | Validation, aspect checks, logging | Pick the character look once; approve first frames |
| Shot generation | Flow (Veo) / Gemini app / Grok Imagine; `packets.py` + `handoff.video_brief` produce per-shot work briefs | Packet text, import validation, draft take | Make the clip in the app, drop the file back |
| QA vs conti | Local checks (duration, aspect, black/silent via `quality_diagnostics`) + per-criterion checklist from the conti | Mechanical checks automatic; each `acceptance_criteria` item marked pass/fail | Confirm the visual criteria — one screen, per shot |
| Targeted revision | Same packet path, re-briefed from the changed conti fields | Diff of the conti, fresh packet, take replacement | Re-make one clip, re-import |
| Assembly/export | Existing `compile-preview` / `compile-final`, FFmpeg, subtitles/font checks | Full | Watch the Preview; approve Final |

At a subscription limit the work waits in queue — the app shows which packets
are outstanding; it never falls back to a paid API (there is none configured).

Human touch points in production: **five** — conti discussion, look approval,
packet make/import per take, per-shot criterion check, final Preview watch. The
current ~10 formal gates (reviews, waves, route decision, spend approvals,
delivery approvals) collapse into per-shot accept + Final approve, with names
recorded as plain records.

## 4. Keep, simplify, freeze, delete

**Keep:** compiler/build/replay (`compiler.py`, `builds.py`), lyric pipeline,
handoff + packets (they are already the subscription-only path), shot board,
review diff, media diagnostics, FFmpeg encode path (owner-waived
`ANIM015-ENC-20261008`), storage/archive core.

**Simplify — approval/proof becomes settings:** single `approver` name in
`project.yaml` settings; per-shot `accept` and whole-film `approve` records keep
reviewer + evidence + digest binding but drop the wave/route/spend/delegation
ceremony. W00's *principle* (hard cut first) is kept; `w00_gate.py`'s record
types are frozen as legacy — still readable, no longer required to proceed.
Qualification facets (UNQUALIFIED/PENDING/NOT_AUTHORIZED) stay honest where
they exist but leave the critical path.

**Freeze:** PR #98 remote integration — leave unmerged or merge on its own
review, but build nothing on the remote worker path until the hard cut proves a
need; `anim-016` hosted-notebook/subscription-code-runtime automation (consumer
ToS forbids automation anyway, `README.md:221`); extra encoder drivers beyond
FFmpeg; `perf_scheduler` advanced paths.

**Delete/deprecate from the default path:** every paid API route — `gemini`,
`fal`, `openart` paid, `economy`/`benchmark` — marked legacy-only in the model
picker; `fake_segment` stays as a test double, never a product path.

**Program nodes:** `anim-023` becomes the closeout that records this pivot;
`film-studio-qualification` waits until a real MV exists to qualify. No new
program nodes until the hard cut's evidence lands.

## 5. Re-ordered roadmap

Acceptance is measured on real outputs, not mocks.

1. **R1 — this PR: detailed conti document.** `engine/conti.py`: per-shot
   `conti/S<id>.json` (scene goal, lyric anchors, beat map, framing, camera
   moves, per-character blocking, lighting, palette, continuity, transitions,
   acceptance criteria, open questions), draft/validate/submit, a paste-out
   discussion request, and `conti-status`. Acceptance: pytest green; validation
   rejects out-of-bounds timing, unknown ids and path escapes; shots, lyrics,
   reviews and LOCK untouched.
2. **R2 — pick the hard cut, finish its conti.** Owner chooses one 5–10 s scene
   of the real song; human+LLM discussion fills its conti to READY.
   Acceptance: `conti-status` READY for the scene's shots on the real project.
3. **R3 — generation brief from conti.** The packet/work-brief for a shot
   carries its conti: first-frame prompt, reference attachments, video prompt
   with motion beats and the acceptance checklist. Acceptance: a real clip made
   in a subscription app imports as a draft take; `handoff_log` records it.
4. **R4 — targeted revision loop.** "Change only the hand gesture": edit the
   conti's blocking field, regenerate the brief, import take two. Acceptance:
   the new take replaces only that shot's draft; all other shots, lyric timing
   and earlier builds are byte-identical.
5. **R5 — hard cut in the film.** Swap the take in, `compile-preview`, watch it
   with the real audio and subs; record `docs/evidence/hard-cut-01/` —
   subscription units spent, wall time, retries, human minutes. Acceptance:
   the Preview plays the real scene; the metrics file is real numbers.
6. **R6 — per-shot QA from conti.** Mechanical checks plus the conti's own
   acceptance_criteria rendered as a checklist; recorded per shot.
7. **R7 — scale the loop** to the rest of the song in shot order; a Preview per
   wave of finished shots.
8. **R8 — Final.** `compile-final` + delivery package for the real song;
   then, and only then, revive qualification scope.

## 6. Risks and open decisions for JunTae

- **Primary video app**: Flow (Veo) vs Gemini app vs Grok Imagine for the hard
  cut — pick one; clip length/aspect limits differ (`README.md:203`).
- **Rate limits unknown**: subscription caps are unmeasured; the first cut's
  evidence file should record them rather than guess.
- **Character consistency without a paid reference API**: Nano Banana-style
  reference sheets made by hand are the proposed path — confirm acceptable.
- **PR #98**: merge or freeze — recommended freeze; decide.
- **Settings location** for the simplified approvals: `project.yaml` vs
  `~/.film_unit` — proposed `project.yaml`, since approval is per-project.
- **Copy-paste volume**: at ~48 shots, is per-shot manual hand-off still
  acceptable, or should packets batch several shots?
