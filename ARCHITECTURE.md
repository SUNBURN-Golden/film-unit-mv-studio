# Architecture

The compiler is a file-based local application. ChatGPT/Work is the director; renderers receive locked shots. No provider receives the whole movie and decides its edit.

## Modules

| Module | Responsibility |
|---|---|
| `core.py` | Atomic metadata writes, asset path bounds, SHA-256 locks, FFmpeg helpers, global frame clock |
| `audio.py` | Decode analysis derivative; librosa tempo/onsets/beats; candidate boundaries; waveform |
| `production.py` | Editable bible drafts, measured beat-aligned cuts, clearly labelled layout placeholders |
| `renderer_router.py` | Capability/configuration-constrained provider preferences |
| `renderers.py` | Common renderer interface; local, imported, Work/OpenArt bridge; normalization |
| `openart_bridge.py` | Store verified forms, references and quotes; bind history IDs; import output receipts |
| `budget.py` | Concrete estimate, batch approval binding, durable conservative reservations |
| `qc.py` | Technical checks, sampled frames, evidence-backed semantic review |
| `pipeline.py` | Resume state, bounded attempts, corrections, cache, gates |
| `assemble.py` | Ordered normalized clips, sole master soundtrack, validation and exports |
| `cli.py` / Streamlit | Operator interfaces over the same engine |

## Timing

Audio is measured once for the entire source, up to 600 seconds. Sequence and shots always cover that measured duration. Pilot compile selects a prefix without mutating the full manifest. Integer milliseconds remain the editorial source of truth. `frame_at(ms) = floor((ms * fps + 500) / 1000)` maps absolute boundaries to the constant frame clock. Clip frame count is the difference of mapped boundaries. Audio remains on its unshifted source timeline.

Structural changes are candidate energy/spectral shifts, not inferred verse or chorus names. The baseline shot plan is a draft. Tempo is a measurement estimate, especially unreliable for rubato, silence or syncopated material.

## Production identity and approval

LOCK stores a digest over master bytes, brief, lyrics, measured analysis, story, bibles, sequence, shot content, reference images and format. Any edit invalidates it. Shot status is not overwritten during render; execution state is stored separately. Test placeholder LOCK cannot authorize real generation.

The estimate binds production digest, effective pilot shot ranges, renderer, quality, exact provider config and retry policy. A stale approval cannot authorize a changed configuration. Estimates reserve the full configured retry count per generated shot. Reservations are durable and idempotent by job ID, and count against a cumulative project cap. Failed or pending jobs retain their reservations; refunds are not guessed. The implementation is single-writer per project via an OS file lock.

Provider prices can change at submission time. Work must obtain a fresh exact price before submitting and stop if it differs from the approved row. Local accounting cannot impose a provider-side credit ceiling across unrelated jobs initiated outside this compiler.

## Render state

Each run has a stable configuration digest. Each attempt has a stable `run_shot_attempt` job ID. Source edits invalidate normalized clip caches. An OpenArt job writes an outbox request and pauses. Work records the returned history ID immediately; uncertain submissions are reconciled against that ID rather than repeated. Completed clips have hash-bound receipts. Network timeouts are not treated as permission to duplicate a billed submission.

Technical or explicitly scored semantic failure adds concrete correction notes to the next attempt. Missing semantic review pauses without consuming a retry. Exhaustion stops that shot without substituting a different scene. Generated clips are stripped of audio, normalized to the target video format, and trimmed to the planned frame count. Short clips are rejected rather than silently stretched or frozen.

## QC meaning

Technical tests inspect exact dimensions, frame rate, frame count, decodability and absence of clip audio. Samples are taken from first, quarter, middle, three-quarter and final actual frame. Small clips may produce fewer distinct samples. Color statistics are diagnostic data, not a character or style score.

Semantic items have no numeric default. A review must contain the normalized clip hash, production digest, reviewer, notes and every requested score. Each dimension must reach the threshold; a high average cannot hide identity failure. A future vision reviewer can write the same schema. No neural visual evaluator, OCR or face matcher is silently claimed to exist.

## Local motion and art

The included mock generator draws an exact layout diagram, not original production art. Approved first frames are supplied through Work or imported. Local video supports hold, pan and zoom; pan/zoom are rejected when the shot says camera movement is none. Split screen can be authored directly into the locked first frame. General layered character animation, per-limb rigging, lip-sync, rotoscoping and dynamic split-screen composition are future features.

## Platform scope

Streamlit is a local control panel, not a hosted SaaS. Python does not possess Work's connector credentials. No invented OpenArt REST endpoint or secret-token extraction is used. Authentication, multiuser concurrency, hosted workers and scheduled jobs are deliberately outside v0.1. Browser access to the local panel was blocked by the Work browser environment; Streamlit's execution testing framework was used instead.
