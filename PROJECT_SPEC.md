# Project specification / v0.1

## Acceptance status

| Stage | Expected | Status in this delivery |
|---|---|---|
| A | 30-second audio → analysis → storyboard → Mock → MP4 | PASS with original synthetic test signal |
| B | Three actual AI-generated shots in the same 30 seconds | NOT RUN; provider bridge schema/receipt lifecycle tested without spending |
| C | 60-second pilot with at least three real generated shots | Mock 60-second export PASS; real-generation portion remains pending |
| D | Full supplied Suno song | Awaiting the user's MP3/WAV and approved production art |

This is an operational Mock-first MVP, not completion of the full real-generation acceptance criterion. No real Suno audio or earlier actual character/storyboard image was attached to this task. Memory and prose do not substitute for those files.

## Files

```text
FILM_UNIT/
  app/control_panel.py
  engine/
  templates/
  docs/
  tests/
  projects/<project>/
    project.yaml
    input/master.wav or master.mp3
    input/lyrics.txt
    input/brief.md
    analysis/audio.json
    analysis/waveform.png
    bible/style_bible.yaml
    bible/story.md
    bible/characters.yaml
    bible/locations.yaml
    characters/
    locations/
    storyboard/S001.png ...
    storyboard/contact_sheet.jpg
    storyboard/storyboard.html
    manifest/sequence.json
    manifest/shots.json
    manifest/locks.json
    render/draft/<run>/
    render/final/<run>/
    render/manual/
    render/requests/
    render/responses/
    render/estimate.json
    render/approval.json
    render/ledger.json
    qc/frames/
    qc/reviews/
    qc/report.json
    output/
```

## Shot schema

Required fields: `id`, `sequence`, `in_ms`, `out_ms`, `duration_ms`, `description`, `characters`, `locations`, `composition`, `camera`, `motion`, `references`, `render_mode`, `renderer`, `status`.

IDs are unique `S001`-style strings. Ranges are contiguous, non-overlapping, integer milliseconds. Duration equals `out_ms - in_ms`. Every included shot needs at least one output frame. `render_mode` is STATIC, LIMITED_MOTION or FULL_GENERATIVE. `renderer: mock` can opt a particular motion shot into local processing in a Manual/OpenArt run; STATIC always stays local. Provider name overrides must exist in the current configured capability set.

## Audio schema

`duration_ms`, estimated `tempo_bpm`, `beat_times_ms`, `onsets_ms`, `energy_curve`, `section_boundaries_ms`, `silence_regions`, `major_peaks_ms`, source hash and measurement settings. Curve points use actual decoded-audio timings. Silence has an absolute -45 dBFS threshold and minimum 250 ms region length. No arbitrary verse labels or lyric alignments are invented.

## Budgets

Default cap: 10,000 credits. Default retries: two additional attempts after the first. Draft: 960×720 local output; final: 1440×1080. Provider-native output size depends on its verified form; final delivery may normalize it. The app prices the selected Draft or Final pass, not a hidden two-pass workflow. Running both consumes two separately approved batches under the same project ledger.

The source brief's sample prices are illustrative and not hardcoded. A read-only OpenArt quote on 2026-09-11 returned 350 credits for one Seedance 2.0 Fast image-to-video job at 720p, 5 seconds, 4:3, audio off. Other settings cost differently and the final price is determined at generation. This sample is not a quote for the entire user's MV and has not been authorized or spent.

## Export contract

Default video: 4:3, 1440×1080, 24fps, H.264, CRF 18, yuv420p. Default audio: AAC 320 kbps from the sole original master input. No loudness normalization, time stretch or generated-clip audio. Exact source file is retained separately. Main output is atomically promoted only after validation; incomplete `.pending.mp4` files are never shown as completed exports.

## Next concrete input

Upload the final Suno MP3/WAV, optionally lyrics and the earlier chosen character/art reference. Work can then build a song-specific production bible and real storyboard, obtain an exact three-shot quote, lock the package, request that one batch approval, and execute the real-render pilot.
