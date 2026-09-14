# v0.3 compiler execution report — 2026-09-14

Base: `BeautifulMind-JT/film-unit-mv-studio` main commit `f12865d5de1dd76ab7973b985b80ae6cdae350d7`.
All 40 restored base files were verified against their Git blob SHA before editing.

## Executed locally

- Original regression suite: **23 passed**.
- Complete v0.3 suite before the final subtitle-font LOCK test: **61 passed in 70.12 s**.
- Final schema/preset/subtitle-font LOCK suite: **8 passed in 1.02 s**, including the additional test. The combined suite now contains 62 tests; the GitHub workflow runs the complete set.
- Python: 3.12.14. FFmpeg: 6.1.1. Editable package installation and Python syntax compilation succeeded.
- Streamlit AppTest: project selection, seven tabs, shot split, unchanged lyrics timing hash, Preview generation, build selection and integrity verification succeeded without UI exceptions. Its output contained 48 frames and 2.000 seconds.

## Whole-song regression

| Measured result | Value |
|---|---|
| Original synthetic PCM duration | 240.000 s |
| Shots | 48 |
| Selected assets | 7 final, 13 draft, 25 storyboard, 3 placeholder |
| Explicitly timed fixture lyrics | 82 lines |
| Clean and subtitled master | 240.000 s, 5,760 frames, 24 fps |
| Test resolution | 320 × 240 |
| Output audio | One track sourced from the original synthetic master |
| Normalized shot audio | None |
| Second build | Only S024 source and clip hashes changed |
| Unchanged normalized clips reused | 47 |
| Audio and lyric timing hashes | Unchanged |
| First and second build inventories | Valid |

The fixture designates local color clips as final/draft selections to exercise the resolver. These are not AI generations. The synthetic lyric timings are explicit test data, not inferred alignment of a singer.

Further executed tests cover corrupt/missing media fallback, strict Final refusing draft assets, missing LOCK or incomplete lyrics, original-audio duration mismatch, source-window changes invalidating approval, replay with the live source project removed, and saved-build tampering detection.

Subtitle tests include a real FFmpeg burn with unchanged AAC packet hashes, source-text/repeat coverage checks, missing Korean glyph rejection and a simulated successful process returning a corrupt MP4. Failed media validation preserves the previous output. Local asset registration failures remain blocked at attempt zero on resume and do not trigger another generation request.

## Limits

No paid video generation was submitted. No provider credits were used. No autonomous semantic vision evaluator or full-song vocal aligner was added. This validates the compiler and editing loop; it does not establish animation quality or completion of the user's real music video. The default export format remains 1440 × 1080 / 24 fps, while this whole-song regression intentionally uses a smaller canvas.

The CI workflow includes Python 3.11 and 3.12. Consult the actual commit's Actions checks for remote results; local execution does not imply that those jobs have run.
