# Project specification / v0.1

## Acceptance status

| Stage | Expected | Status in this delivery |
|---|---|---|
| A | 30-second audio → analysis → storyboard → Mock → MP4 | PASS with original synthetic test signal |
| B | Three actual AI-generated shots in the same 30 seconds | NOT RUN; provider bridge schema/receipt lifecycle tested without spending |
| C | 60-second animated pilot: actual motion for every animated shot, including at least three provider-generated shots | Mock 60-second export PASS; finished animated pilot remains pending |
| D | Full supplied Suno song | Awaiting the user's MP3/WAV and approved production art |

This is an operational Mock-first MVP, not completion of the full real-generation acceptance criterion. No real Suno audio or earlier actual character/storyboard image was attached to this task. Memory and prose do not substitute for those files.

## Animation direction (2026-09-11 clarification)

The deliverable is a 2D character-animation music video. Characters, expressions, props and environmental elements perform the approved actions within each shot. A locked camera is compatible with animated subjects. Economical acting does not mean replacing most of the film with camera motion over still images; a stop-motion aesthetic is not the target.

New production packages draft every shot as LIMITED_MOTION. The director upgrades complex actions to FULL_GENERATIVE and may deliberately choose STATIC for an editorial hold. There is no automatic 40% STATIC quota. Existing packages and their approvals are not rewritten. Mock hold/pan/zoom remains a timing-preview tool and does not satisfy final animation acceptance. LIMITED_MOTION is not automatically a free local effect: the current local renderer has no character rigging or subject-animation engine.

For a four-minute film, initial budget planning therefore covers 240 seconds of animation until the actual shot plan establishes any intentional holds or reuse. At the quoted 350 credits per five-second job, 48 jobs cost 16,800 credits for a first pass; 15 additional jobs provide about 30% retry allowance for a total of 22,050. The current two-retry-per-shot reservation policy instead reserves up to 50,400. These are video-only planning scenarios at 720p with audio off, not a final 1080p quote or a guarantee of successful takes. Storyboard/reference generation and unused clip portions add cost.

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


## 저가 애니메이션 renderer 추가 (2026-09-11)

fal Wan 2.2 Turbo adapter와 OpenArt PixVerse V6 schema 지원을 추가했다. fal USD 예산은 OpenArt credits와 별도 한도·예약액으로 관리한다. 비동기 제출 전에 상태를 저장하고, 응답이 불확실하면 배치를 중단해 중복 결제를 방지한다. 3×5초의 실제 인물 동작 비교 입력을 생성하는 benchmark 명령을 제공한다. 현재 유료 생성은 0건이며 실제 품질 검증이 남았다. 세부 설정·한계·증거는 [저가 애니메이션 테스트](docs/CHEAP_ANIMATION_TEST.md)를 참고한다.
