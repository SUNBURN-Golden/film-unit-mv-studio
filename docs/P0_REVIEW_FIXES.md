# PR #1 review fixes — 2026-09-14

Reviewed base: `cfdfcf7c3ac68f27fb1d4f70699c4125c0854d4b`.

## Reproduced before correction

The initial three dependency regressions failed for neighbor-cut and lyric-timing edits; shared-style invalidation already passed. Expanded tests additionally reproduced stale own-reference approval and subtitle settings/font changes failing to require lyric re-review (five failing, two passing cases before corrections).

## Corrected behavior

| Edit | Visual review | Lyric review | Global production LOCK |
|---|---|---|---|
| Move one cut | Adjacent shots expire; unrelated shots survive | Preserved | Renew |
| Change lyric timing | Preserved | Renew | Renew |
| Change subtitle settings/font | Preserved | Renew | Renew |
| Replace one first frame | That shot expires | Preserved | Renew |
| Change shared style/cast/story/source lyric wording | All visual reviews expire | Source wording changes also expire lyric review | Renew |
| Change a take's source window | That selection expires | Preserved | Existing timeline LOCK can remain |

Shared visual context excludes the visual timeline and subtitle timing/settings. Per-shot review includes the actual reference bytes, shot definition, video SHA and source window. Renderer QC uses this same scope. Registering a reviewed normalized clip verifies the complete QC binding before transferring approval to the clip's zero-based local window, so edits between inspection and registration cannot inherit the old approval. A mismatch blocks the render batch without consuming a generation retry.

Lyric review schema 2 adds subtitle settings, canvas/FPS and actual font SHA/family to the existing source/cue fingerprint. Validation is repeated at Final export completion. Old global-only video reviews and timing-only lyric reviews lack this proof and require one explicit re-review; no historical build is rewritten or automatically reauthorized.

Split now creates independent first-frame byte copies under the new shot's own path. Imports replace only owned artwork and detach legacy shared references before changing their common file. Orphan/colliding destinations are protected. Tests replace either side and check the peer's SHA remains unchanged. Extra character/location references remain shared intentionally.

Build lists now sort the numeric suffix: B10000 precedes B9999 in newest-first views.

## Execution evidence

- Full local suite: **92 passed in 80.14 s**.
- Additional QC-to-registration regression suite: **6 passed in 5.24 s**.
- Combined CI collection: **98 tests**, including the existing 240-second/48-shot/82-line MP4 regression.
- Scoped approval tests perform actual small synthetic Final compilations after lyric edits; they also verify current LOCK remains required.
- All media fixtures are local/synthetic. No paid generation or real-song alignment was submitted.

The GitHub Actions run for the resulting commit executes the combined suite under Python 3.11 and 3.12. Consult that run for remote results; the local counts above are separate execution records.
