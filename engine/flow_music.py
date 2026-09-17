"""Manual Google Flow Music bridge.

This module never calls Google or any paid provider. It exports a self-contained
creative package for a human-operated Flow Music session, then can register a
downloaded whole-song result back against the existing shot timeline. FILM UNIT
keeps the original master as the final audio source.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from .core import FilmError, digest, now, probe, read, safe_path, timecode, validate_manifest, write
from .resolver import register_asset


FLOW_EXPORT_SCHEMA = 1


def _text(path: Path, required: bool = True) -> str:
    if not path.is_file():
        if required:
            raise FilmError(f"Missing file: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def _pretty(value) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)


def _allocate_export(project: Path) -> Path:
    root = project / "exports" / "flow_music"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(1, 100000):
        candidate = root / f"F{number:04d}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise FilmError("Flow export ID space exhausted")


def _source_context(project: Path):
    config = read(project / "project.yaml")
    audio = read(project / "analysis/audio.json")
    shots = read(project / "manifest/shots.json")
    sequences = read(project / "manifest/sequence.json")
    validate_manifest(shots, audio["duration_ms"], config["format"]["fps"])

    master = safe_path(project, config["audio"]["path"])
    master_sha = digest(master)
    if master_sha != config["audio"]["sha256"]:
        raise FilmError("Master audio hash mismatch")
    if audio.get("master_sha256", master_sha) != master_sha:
        raise FilmError("Audio analysis belongs to a different master")

    measured_ms = round(float(probe(master)["format"].get("duration", 0)) * 1000)
    if abs(measured_ms - audio["duration_ms"]) > 100:
        raise FilmError("Analysis duration does not match the actual master audio")

    return config, audio, master, sequences, shots


def _master_prompt(project: Path, config, audio, sequences, shots) -> str:
    lyrics = _text(project / "input/lyrics.txt", required=False)
    brief = _text(project / "input/brief.md")
    story = _text(project / "bible/story.md")
    style = read(project / "bible/style_bible.yaml")
    characters = read(project / "bible/characters.yaml")
    locations = read(project / "bible/locations.yaml")
    directing = read(project / "bible/directing.yaml", {})

    sequence_lines = []
    for seq in sequences:
        sequence_lines.append(
            f"- {seq['id']}  {timecode(seq['in_ms'])}–{timecode(seq['out_ms'])}: {seq.get('title', '')}"
        )

    shot_lines = []
    for shot in shots:
        shot_lines.append(
            f"- {shot['id']}  {timecode(shot['in_ms'])}–{timecode(shot['out_ms'])} | "
            f"{shot['description']} | composition={shot['composition']} | "
            f"camera={_pretty(shot['camera']).replace(chr(10), ' ')} | "
            f"motion={_pretty(shot['motion']).replace(chr(10), ' ')}"
        )

    lyrics_block = lyrics.rstrip() if lyrics.strip() else "[No lyrics supplied. Do not invent lyrics or captions.]"
    return f"""# FILM UNIT → Google Flow Music master prompt

Create a coherent music video for the attached ORIGINAL MUSIC FILE. The uploaded audio is the timing authority.
Do not rewrite, remix, replace, extend, shorten, time-stretch, or regenerate the song. Build the visuals around it.

## Non-negotiable continuity
- Preserve the same character identities, wardrobe logic, locations and art direction across the whole video.
- Follow the supplied story, style, character and location bibles. Do not substitute a generic AI-commercial look.
- Treat supplied storyboard/reference images as visual continuity references when they are attached.
- Respect the time ranges and intended action below as closely as the generation system allows.
- Do not render lyric captions, logos or arbitrary on-screen text unless the shot direction explicitly requires them.
  FILM UNIT adds reviewed subtitles after generation.
- Keep important faces, hands and props stable across cuts. Avoid identity drift and unexplained costume/location changes.
- Prefer motivated cinematic cuts and visual escalation at musical changes over constant camera motion.

## Output intent
Project format: {config['format']['width']}×{config['format']['height']} at {config['format']['fps']} fps, aspect {config['format'].get('aspect_ratio', 'project-defined')}.
Measured song duration: {audio['duration_ms']} ms ({timecode(audio['duration_ms'])}).
The downloaded Flow result will be re-cut by FILM UNIT on the exact shot boundaries below and its generated audio will be discarded; the original master will be muxed back in.

## Creative brief
{brief.rstrip()}

## Story bible
{story.rstrip()}

## Style bible
```json
{_pretty(style)}
```

## Character bible
```json
{_pretty(characters)}
```

## Location bible
```json
{_pretty(locations)}
```

## Directing bible
```json
{_pretty(directing)}
```

## Sequence map
{chr(10).join(sequence_lines)}

## Exact shot map
{chr(10).join(shot_lines)}

## Source lyrics — preserve wording; do not invent missing lines
```text
{lyrics_block}
```

Generate one continuous music video synchronized to the attached original song. Maintain visual continuity through the whole piece. When the shot map is more specific than a generic stylistic impulse, follow the shot map.
"""


def _shot_prompt(project: Path, shot) -> str:
    style = read(project / "bible/style_bible.yaml")
    characters = read(project / "bible/characters.yaml")
    locations = read(project / "bible/locations.yaml")
    refs = shot.get("references", [])
    return f"""# {shot['id']} — {timecode(shot['in_ms'])} → {timecode(shot['out_ms'])}

Create or refine footage for this exact music-video beat. Use the attached reference image for this shot as the composition/identity anchor when available.

Description: {shot['description']}
Characters: {_pretty(shot.get('characters', []))}
Locations: {_pretty(shot.get('locations', []))}
Composition: {shot['composition']}
Camera: {_pretty(shot['camera'])}
Motion: {_pretty(shot['motion'])}
Render mode: {shot['render_mode']}
Reference files in the export bundle: {_pretty(refs)}

Continuity requirements:
- Preserve established identities and the approved visual language.
- Do not create captions or unrelated text.
- Do not invent another scene, location, wardrobe change or camera move that conflicts with this shot.
- The original song owns timing; visual action should resolve inside this shot window.

Style context:
{_pretty(style)}

Character context:
{_pretty(characters)}

Location context:
{_pretty(locations)}
"""


def _copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if digest(source) != digest(target):
        raise FilmError(f"Export copy hash mismatch: {source}")


def export_flow_music(project, output=None):
    """Create an append-only, no-provider-call package for Google Flow Music."""
    p = Path(project).resolve()
    config, audio, master, sequences, shots = _source_context(p)
    bundle = Path(output).resolve() if output else _allocate_export(p)
    if output:
        if bundle.exists() and any(bundle.iterdir()):
            raise FilmError("Flow export output directory must be empty")
        bundle.mkdir(parents=True, exist_ok=True)

    try:
        audio_target = bundle / "audio" / master.name
        _copy(master, audio_target)
        lyrics = p / "input/lyrics.txt"
        if lyrics.is_file():
            _copy(lyrics, bundle / "lyrics.txt")

        context_files = [
            "input/brief.md", "analysis/audio.json", "manifest/sequence.json", "manifest/shots.json",
            "bible/story.md", "bible/style_bible.yaml", "bible/characters.yaml", "bible/locations.yaml",
            "bible/directing.yaml",
        ]
        for relative in context_files:
            source = safe_path(p, relative)
            if source.is_file():
                _copy(source, bundle / "context" / relative)

        copied_assets = []
        missing_assets = []
        relatives = set()
        for shot in shots:
            relatives.update(shot.get("references", []))
        for directory in ("characters", "locations"):
            relatives.update(str(path.relative_to(p)) for path in (p / directory).rglob("*") if path.is_file())
        for relative in sorted(relatives):
            source = safe_path(p, relative)
            if source.is_file():
                _copy(source, bundle / "assets" / relative)
                copied_assets.append(relative)
            else:
                missing_assets.append(relative)

        (bundle / "shots").mkdir(exist_ok=True)
        for shot in shots:
            (bundle / "shots" / f"{shot['id']}.md").write_text(_shot_prompt(p, shot), encoding="utf-8")
        (bundle / "MASTER_PROMPT.md").write_text(
            _master_prompt(p, config, audio, sequences, shots), encoding="utf-8"
        )
        (bundle / "FLOW_INSTRUCTIONS.md").write_text(
            f"""# Google Flow Music trial instructions

This package makes no API call and spends no provider credits by itself.

1. Open Google Flow Music → Music videos → New music video.
2. In the prompt box, choose Add → Audio and upload `audio/{master.name}`.
3. Paste the complete contents of `MASTER_PROMPT.md` into the prompt box.
4. Add selected images from `assets/` when they materially help identity, location or composition continuity.
5. Choose the video-generation model/settings you want in Flow and generate the music video.
6. Download the resulting video without modifying the project master audio.
7. Register that whole-song result with FILM UNIT:

   `filmunit flow-import-result {p} /path/to/flow_result.mp4 --kind draft`

8. Run `filmunit compile-preview {p}`. FILM UNIT will slice the downloaded video on the existing exact shot boundaries, discard its audio, and mux the original project master back in.
9. Review the preview. Final approval remains subject to the existing per-shot review and production LOCK rules.

Missing optional/reference assets at export time: {len(missing_assets)}
""",
            encoding="utf-8",
        )

        source_hashes = {
            "project.yaml": digest(p / "project.yaml"),
            config["audio"]["path"]: digest(master),
            "analysis/audio.json": digest(p / "analysis/audio.json"),
            "manifest/sequence.json": digest(p / "manifest/sequence.json"),
            "manifest/shots.json": digest(p / "manifest/shots.json"),
        }
        for relative in ["input/brief.md", "input/lyrics.txt", "bible/story.md", "bible/style_bible.yaml",
                         "bible/characters.yaml", "bible/locations.yaml", "bible/directing.yaml"]:
            source = safe_path(p, relative)
            if source.is_file():
                source_hashes[relative] = digest(source)

        export_hashes = {}
        for file in sorted(bundle.rglob("*")):
            if file.is_file() and file.name != "manifest.json":
                export_hashes[str(file.relative_to(bundle))] = digest(file)
        manifest = {
            "schema_version": FLOW_EXPORT_SCHEMA,
            "export_id": bundle.name,
            "created_at": now(),
            "source_project": str(p),
            "audio_policy": "Flow receives the original master for generation context; FILM UNIT discards downloaded audio and restores the original master during compile.",
            "provider_submission": "MANUAL_ONLY",
            "paid_provider_calls_made": 0,
            "duration_ms": audio["duration_ms"],
            "shot_count": len(shots),
            "copied_assets": copied_assets,
            "missing_assets": missing_assets,
            "source_hashes": source_hashes,
            "export_hashes": export_hashes,
        }
        write(bundle / "manifest.json", manifest)
        return {
            "status": "READY_FOR_MANUAL_FLOW_MUSIC",
            "export_id": bundle.name,
            "bundle": str(bundle),
            "master_prompt": str(bundle / "MASTER_PROMPT.md"),
            "audio": str(audio_target),
            "shots": len(shots),
            "missing_assets": missing_assets,
            "paid_provider_calls": 0,
        }
    except Exception:
        # An allocated export is never left looking complete after a failed build.
        if bundle.exists() and not (bundle / "manifest.json").exists():
            shutil.rmtree(bundle, ignore_errors=True)
        raise


def import_flow_result(project, source, kind="draft", reviewer="", evidence=""):
    """Bind one whole-song Flow result to every existing shot using exact source windows.

    No time stretching or offset guessing is performed. The source must cover the
    measured project duration closely enough to preserve the original timeline.
    """
    p = Path(project).resolve()
    source = Path(source).resolve()
    if not source.is_file():
        raise FilmError("Flow result file is missing")
    if kind not in {"draft", "final"}:
        raise FilmError("Flow result kind must be draft or final")
    if kind == "final" and (not reviewer.strip() or not evidence.strip()):
        raise FilmError("Final Flow import requires reviewer and evidence")

    config, audio, _, _, shots = _source_context(p)
    info = probe(source)
    videos = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"]
    if len(videos) != 1:
        raise FilmError("Flow result must contain exactly one video stream")
    source_ms = round(float(info["format"].get("duration", 0)) * 1000)
    tolerance_ms = max(250, round(1000 / config["format"]["fps"]))
    if abs(source_ms - audio["duration_ms"]) > tolerance_ms:
        raise FilmError(
            f"Flow result duration differs from the measured master by more than {tolerance_ms} ms; "
            "FILM UNIT will not silently retime it"
        )

    # register_asset copies identical bytes only once; every shot then points at a
    # different exact source window in the same immutable local asset.
    imported = []
    for shot in shots:
        record = register_asset(
            p, shot, source, kind,
            reviewer=reviewer,
            evidence=evidence,
            source_in_ms=shot["in_ms"],
            generated=True,
        )
        imported.append({"shot": shot["id"], "source_in_ms": shot["in_ms"], "sha256": record["sha256"]})
    return {
        "status": "IMPORTED",
        "kind": kind,
        "source": str(source),
        "source_sha256": digest(source),
        "duration_ms": source_ms,
        "shots_registered": len(imported),
        "windows": imported,
        "audio_policy": "Downloaded Flow audio is ignored by compile; original project master remains authoritative.",
    }
