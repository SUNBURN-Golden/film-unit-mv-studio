# Google Flow Music trial bridge

This branch tests a manual Google Flow Music workflow without adding a Gemini/Flow API dependency or changing FILM UNIT's source-of-truth rules.

Google Flow Music supports creating a music video from an uploaded audio file through **Music videos → New music video → Add → Audio**. FILM UNIT uses that UI as a human-operated renderer: it prepares the exact original song, creative context and prompt package; Google generates a whole-song candidate; FILM UNIT imports the downloaded result as visual source windows and restores the original project master during compilation.

## Export

```bash
filmunit flow-export projects/MY_PROJECT
```

The command creates an append-only bundle under `exports/flow_music/F####/` containing:

- `audio/master.*` — byte-identical project master
- `lyrics.txt` — exact user-supplied lyric source when present
- `MASTER_PROMPT.md` — whole-song Flow Music prompt
- `shots/S###.md` — exact shot-window prompts for refinement
- `assets/` — existing storyboard/character/location references
- `context/` — brief, bibles, analysis and manifest snapshots
- `manifest.json` — source/export hashes, duration, shot count and an explicit `paid_provider_calls_made: 0`

Export does not require production LOCK and does not submit anything to Google.

## Generate in Google Flow Music

1. Open Google Flow Music and create a new music video.
2. Add the exported `audio/master.*` as the audio reference.
3. Paste `MASTER_PROMPT.md`.
4. Add only the image references that materially help continuity.
5. Generate and download the whole-song result.

The generated file is treated as a candidate visual track, not as a new audio master.

## Import the whole result

```bash
filmunit flow-import-result projects/MY_PROJECT ~/Downloads/flow-result.mp4 --kind draft
filmunit compile-preview projects/MY_PROJECT
```

`flow-import-result` requires the downloaded video's duration to match the measured master within a small container tolerance. It does **not** time-stretch or guess an offset. The same immutable downloaded file is registered for every shot with `source_in_ms` equal to that shot's absolute `in_ms`.

During Preview compilation, each shot is cut from that visual source on FILM UNIT's exact existing boundaries. Clip audio is discarded and the original project master is muxed back in, preserving the compiler's audio authority.

A `--kind final` import requires both `--reviewer` and `--evidence`; normal Final production LOCK and scoped review checks still apply.

## What this trial deliberately does not do

- no Google/Gemini REST or SDK call
- no extraction or storage of Google account credentials
- no automatic credit spending
- no replacement of `input/master.*` or `input/lyrics.txt`
- no model-generated rewrite of story, lyrics or shot timing
- no claim that Flow obeys millisecond cut instructions deterministically

The purpose is to test whether Flow Music can supply useful whole-song visual material while FILM UNIT retains deterministic timing, provenance, subtitles, review and final assembly.
