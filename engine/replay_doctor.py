"""film-replay-doctor — replay/rebuild diagnosis and credential-free packets.

목표: 과거 build를 재생하거나 재구축할 수 없는 원인을 정확히 찾는다.

`diagnose_build` inspects a sealed build directory (Build 1 or Build 2
`animation_build`) and reports *why* it can or cannot be replayed or
rebuilt:

- **manifest linkage**: the source → recipe → clean → subbed → encode chain
  is checked end to end — snapshot audio/edit/registry/frame_map digests,
  the sealed sequence roots, subtitle/lyric bindings and the output MP4
  artifact hashes, plus an optional `--deep` recompose of the clean root
  from the archived members (the same deterministic compose replay uses).
- **classification**: every finding is one of MISSING_ASSET,
  HASH_MISMATCH, TOOL_VERSION_DIFF, UNSUPPORTED_PROFILE, SCHEMA_INVALID,
  INCOMPLETE_BUILD or ARCHIVE_UNAVAILABLE — never a bare "failed".
- **branches**: `REPRODUCE_SAME` (verified inputs and a matching toolchain)
  vs `REPRODUCE_NEW_RESULT` (inputs intact but toolchain/profile drifted —
  a rebuild or re-encode produces a *new artifact* and is never reported as
  the same build) vs `BLOCKED` with the blocking classes listed.
- **rebuild check**: when the build sits under `<project>/builds/` the live
  project inputs are compared layer by layer (audio, edit, source pins,
  lyrics, format) — changed layers mean a rebuild yields `NEW_RESULT`,
  never the old build id again.

`diagnose_archive` does the same for a `storage_archive` manifest + FAV1
pack: pinned index hash, index bounds/schema, object presence and member
hashes. Its restore plan is honest about scope: `MEMBER_RANGES` fetches
exactly the selected member ranges (unrequested bytes are reported and are
zero), while `WHOLE_PACK_VERIFIED` is an *explicit* branch that requires a
predeclared byte cap — a bounded restore never silently becomes a
full-source download (`RANGE_UNSUPPORTED`/`CAPACITY_BLOCKED` instead).

`build_replay_packet` writes a CANON_JSON_V1 `replay_packet` document
pinning every input, recipe digest, sealed toolchain and expected output
hash needed to reproduce — plus the credential contract: secrets are never
embedded (`embedded: "NEVER"`); a `DRIVE_BOUNDED` reproduction lists the
required grant *classes* only. `validate_replay_packet` enforces the fixed
field set and rejects any key or value shaped like a token/secret.

`compare_replay_output` compares a produced replay directory against the
sealed inventory: differing bytes are `NEW_ARTIFACT`, never "the same
build". All fixtures are synthetic/local; facets stay UNQUALIFIED /
PENDING / NOT_AUTHORIZED.
"""
from pathlib import Path
import hashlib
import platform
import re

from .animation_schema import (canon_bytes, check_document, read_canon,
                               write_canon)
from .archive_manifest import validate_archive
from .core import FilmError, digest, now, read, safe_path
from .fav_pack import member_for_frame, validate_index
from .pack_reader import PackReader, fetch_index

# --- problem classes (구현 범위 2: 분류) --------------------------------------

MISSING_ASSET = "MISSING_ASSET"          # inventoried/pinned bytes absent
HASH_MISMATCH = "HASH_MISMATCH"          # bytes present but hash differs
TOOL_VERSION_DIFF = "TOOL_VERSION_DIFF"  # sealed vs current toolchain drift
UNSUPPORTED_PROFILE = "UNSUPPORTED_PROFILE"  # unknown profile/document class
SCHEMA_INVALID = "SCHEMA_INVALID"        # document fails its schema/canon gate
INCOMPLETE_BUILD = "INCOMPLETE_BUILD"    # not COMPLETE / no inventory
ARCHIVE_UNAVAILABLE = "ARCHIVE_UNAVAILABLE"  # remote objects unreachable
INDEX_FAULT = "INDEX_FAULT"              # pack_index pin/schema/bounds
RANGE_FAULT = "RANGE_FAULT"              # range-read contract violations
PACK_FAULT = "PACK_FAULT"                # member bytes fail the pinned hash

PROBLEM_CLASSES = (MISSING_ASSET, HASH_MISMATCH, TOOL_VERSION_DIFF,
                   UNSUPPORTED_PROFILE, SCHEMA_INVALID, INCOMPLETE_BUILD,
                   ARCHIVE_UNAVAILABLE, INDEX_FAULT, RANGE_FAULT,
                   PACK_FAULT)

# --- reproduce branches (구현 범위 3) -----------------------------------------

REPRODUCE_SAME = "REPRODUCE_SAME"              # verified inputs + toolchain
REPRODUCE_NEW_RESULT = "REPRODUCE_NEW_RESULT"  # new artifact — not this build
BLOCKED = "BLOCKED"                            # cannot reproduce

# restore plan branches
RESTORE_MEMBER_RANGES = "MEMBER_RANGES"        # bounded ranges only
RESTORE_WHOLE_PACK = "WHOLE_PACK_VERIFIED"     # explicit predeclared branch
RESTORE_BLOCKED = "BLOCKED"

# rebuild-against-live-project branches
REBUILD_SAME_INPUTS = "SAME_INPUTS"
REBUILD_NEW_RESULT = "NEW_RESULT"
REBUILD_IMPOSSIBLE = "IMPOSSIBLE"
REBUILD_UNKNOWN = "UNKNOWN"

# artifact verdicts in compare_replay_output
ARTIFACT_IDENTICAL = "IDENTICAL"          # same bytes as sealed
ARTIFACT_NEW = "NEW_ARTIFACT"             # different bytes — a new artifact
ARTIFACT_EXTRA = "EXTRA"                  # not part of the sealed build
ARTIFACT_MISSING = "NOT_PRODUCED"         # expected output was not produced

# deliverable expectations recorded in the packet
EXPECT_PRESERVED = "BYTES_PRESERVED"                  # copied verbatim
EXPECT_RECOMPOSE = "RECOMPOSE_ROOT_EQUAL"             # root must recompute
EXPECT_REENCODE = "CONTENT_EQUAL_BYTES_MAY_DIFFER"    # toolchain-dependent

_FACETS = {"node_state": "IN_PROGRESS",
           "qualification_state": "UNQUALIFIED",
           "acceptance_state": "PENDING",
           "release_state": "NOT_AUTHORIZED"}

_BUILD_ID = re.compile(r"B[0-9]{4,}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

# A replay packet carries no field that could hold a credential. The fixed
# field set is itself the no-secrets guarantee; this scan is the belt.
_SECRET_KEY_ALLOWED = {"credentials", "credential_epoch"}
_SECRET_KEY_RE = re.compile(
    r"(?i)(token|secret|passw|authoriz|bearer|cookie|oauth|session|"
    r"api_?key|apikey|private_?key|client_?secret|credential)")
_SECRET_VALUE_RES = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"\bya29\.[0-9a-zA-Z_-]+"),          # Google OAuth access token
    re.compile(r"\bAIza[0-9a-zA-Z_-]{20,}"),        # Google API key
    re.compile(r"\b(?:xox[bapors]|ghp|gho|sk|pat)"
               r"[-_][0-9a-zA-Z_-]{8,}"),
]


def _problem(cls, detail, *, path=None, blocking=True):
    if cls not in PROBLEM_CLASSES:
        raise FilmError(f"Unknown problem class: {cls}")
    return {"class": cls, "detail": detail,
            "path": path, "blocking": blocking}


def current_toolchain(ffmpeg_version=None):
    """The toolchain this environment would reproduce with."""
    if ffmpeg_version is None:
        from .perf_scheduler import _ffmpeg_version
        ffmpeg_version = _ffmpeg_version()
    return {"python": platform.python_version(),
            "ffmpeg": ffmpeg_version,
            "compiler": "0.3.0"}


# --- build diagnosis ------------------------------------------------------------

def _sha_ok(value):
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _record(build):
    path = build / "build.json"
    if not path.is_file():
        raise FilmError(f"Missing file: {path}")
    try:
        return read(path)
    except FilmError:
        raise
    except Exception as e:
        raise FilmError(f"build.json is not readable JSON: {e}") from e


def _kind(record):
    """Build 1 (legacy schema 1, no document_type) or Build 2."""
    doc_type = record.get("document_type")
    if doc_type is None:
        return "BUILD_1" if record.get("schema_version") == 1 else None
    if doc_type == "animation_build" and record.get("schema_version") == 2:
        return "BUILD_2"
    return None


def _inventory(build, record, problems):
    """Per-file integrity against the sealed SHA-256 inventory."""
    files = record.get("files")
    if type(files) is not dict or not files:
        problems.append(_problem(
            INCOMPLETE_BUILD, "sealed build has no file inventory"))
        return {"checked": 0, "entries": 0}
    checked = 0
    for relative, expected in sorted(files.items()):
        try:
            asset = safe_path(build, relative)
        except FilmError:
            problems.append(_problem(MISSING_ASSET,
                                     "inventory path escapes the build",
                                     path=relative))
            continue
        if not asset.is_file():
            problems.append(_problem(MISSING_ASSET,
                                     "inventoried file is missing",
                                     path=relative))
            continue
        checked += 1
        if not _sha_ok(expected) or digest(asset) != expected:
            problems.append(_problem(HASH_MISMATCH,
                                     "file bytes differ from the sealed "
                                     "inventory", path=relative))
    return {"checked": checked, "entries": len(files)}


def _toolchain(record, current, problems):
    sealed = record.get("toolchain")
    if type(sealed) is not dict or not sealed:
        problems.append(_problem(
            TOOL_VERSION_DIFF, "the sealed build recorded no toolchain — "
            "byte-identical re-encode cannot be claimed", blocking=False))
        return {"sealed": None, "current": current, "matches": False,
                "differences": ["toolchain"]}
    diffs = []
    for key in ("python", "ffmpeg", "compiler"):
        if sealed.get(key) != current.get(key):
            diffs.append(key)
            problems.append(_problem(
                TOOL_VERSION_DIFF,
                f"{key}: sealed {sealed.get(key)!r} vs current "
                f"{current.get(key)!r} — a re-encode produces new bytes",
                blocking=False))
    return {"sealed": dict(sealed), "current": dict(current),
            "matches": not diffs, "differences": diffs}


def _hash_link(report, problems, build, name, relative, expected, *,
               what="file"):
    """One link of the sealed chain: file exists and matches its pin."""
    entry = {"link": name, "expected_sha256": expected}
    if not _sha_ok(expected):
        entry.update(ok=False, detail=f"{name} records no sha256")
        problems.append(_problem(SCHEMA_INVALID, entry["detail"]))
    else:
        try:
            path = safe_path(build, relative)
        except FilmError:
            path = None
        if path is None or not path.is_file():
            entry.update(ok=False, detail=f"{name}: {relative} is missing")
            problems.append(_problem(MISSING_ASSET, entry["detail"],
                                     path=relative))
        elif digest(path) != expected:
            entry.update(ok=False,
                         detail=f"{name}: {relative} bytes differ from the "
                                "sealed hash")
            problems.append(_problem(HASH_MISMATCH, entry["detail"],
                                     path=relative))
        else:
            entry.update(ok=True, detail=f"{name} pinned and intact")
    report["linkage"].append(entry)


def _linkage_build1(build, record, report, problems):
    """source→clip→audio→subtitle links of a legacy sealed build."""
    audio = record.get("audio") or {}
    _hash_link(report, problems, build, "source:master_audio",
               audio.get("path") or "", audio.get("sha256"))
    for shot in record.get("shots") or []:
        sid = shot.get("id", "?")
        _hash_link(report, problems, build, f"source:{sid}",
                   shot.get("source_path") or "", shot.get("source_sha256"))
        _hash_link(report, problems, build, f"clip:{sid}",
                   shot.get("clip_path") or "", shot.get("clip_sha256"))
    lyrics = record.get("lyrics") or {}
    for field, rel in (("source_sha256", "lyrics_source.txt"),
                       ("timing_sha256", "lyrics_timed.json")):
        if lyrics.get(field):
            _hash_link(report, problems, build, f"lyrics:{field}",
                       rel, lyrics[field])
    report["linkage"].append({
        "link": "recipe:format", "ok": type(record.get("format")) is dict
        and type(record.get("duration_ms")) is int,
        "detail": "legacy concat recipe = captured normalized clips + "
                  "master audio + captured ASS"})


def _linkage_build2(build, record, report, problems, deep):
    """source/recipe/clean/subbed/encode links of a Build 2 seal."""
    snapshot = build / "snapshot"
    audio = record.get("audio") or {}
    _hash_link(report, problems, build, "source:master_audio",
               audio.get("path") or "", audio.get("sha256"))

    # recipe: the pinned edit and the sealed frame_map
    edit_path = snapshot / "timeline/edit.json"
    document = None
    try:
        document = read_canon(edit_path)
        from .animation_review import edit_digest
        ok = edit_digest(document) == record.get("edit_digest")
        report["linkage"].append(
            {"link": "recipe:edit_digest", "ok": ok,
             "detail": "snapshot timeline digest "
                       + ("matches" if ok else "differs from")
                       + " the sealed edit_digest"})
        if not ok:
            problems.append(_problem(
                HASH_MISMATCH, "snapshot timeline/edit.json is not the "
                "sealed edit", path="snapshot/timeline/edit.json"))
    except FilmError as e:
        problems.append(_problem(SCHEMA_INVALID,
                                 f"snapshot timeline/edit.json: {e}",
                                 path="snapshot/timeline/edit.json"))
        report["linkage"].append({"link": "recipe:edit_digest", "ok": False,
                                  "detail": str(e)})

    registry = None
    try:
        from .animation_assets import validate_registry
        registry = validate_registry(
            read_canon(snapshot / "manifest/animation_assets.json"))
    except FilmError as e:
        problems.append(_problem(
            SCHEMA_INVALID, f"snapshot asset registry: {e}",
            path="snapshot/manifest/animation_assets.json"))

    # source: every entry's pin must exist in the archived registry and its
    # member bytes must hash to the pinned revision.
    members_ok = True
    if registry is not None:
        for entry in record.get("entries") or []:
            shot_id, iid = entry.get("shot_id"), entry.get("instance_id")
            pin = registry["assignments"].get(shot_id)
            want = {"asset_id": entry.get("asset_id"),
                    "revision": entry.get("revision"),
                    "content_sha256": entry.get("content_sha256")}
            if pin is None or not all(pin.get(k) == v
                                      for k, v in want.items() if v is not
                                      None):
                problems.append(_problem(
                    HASH_MISMATCH,
                    f"{iid}/{shot_id}: archived registry pin does not "
                    "match the sealed entry"))
                members_ok = False
                continue
            revision = registry["assets"][pin["asset_id"]]["revisions"][
                str(pin["revision"])]
            for f in revision["files"]:
                rel = f["relative_name"]
                try:
                    member = safe_path(snapshot, rel)
                except FilmError:
                    member = None
                if member is None or not member.is_file():
                    problems.append(_problem(
                        MISSING_ASSET,
                        f"{shot_id} member {f['frame_index']} missing",
                        path=f"snapshot/{rel}"))
                    members_ok = False
                elif digest(member) != f["sha256"]:
                    problems.append(_problem(
                        HASH_MISMATCH,
                        f"{shot_id} member {f['frame_index']} bytes differ "
                        "from the pinned revision", path=f"snapshot/{rel}"))
                    members_ok = False
    report["linkage"].append({
        "link": "source:members", "ok": members_ok,
        "detail": "every entry pin resolves in the archived registry and "
                  "member bytes hash to the pinned revision"})

    # recipe: frame_map file + canonical rows + contiguous delivery names
    rows = None
    fmap = build / (record.get("frame_map") or "frame_map.jsonl")
    if not fmap.is_file():
        problems.append(_problem(MISSING_ASSET, "frame_map.jsonl missing",
                                 path="frame_map.jsonl"))
    else:
        _hash_link(report, problems, build, "recipe:frame_map",
                   "frame_map.jsonl", record.get("frame_map_sha256"))
        try:
            from .animation_compiler import _read_frame_map
            rows = _read_frame_map(fmap)
            if len(rows) != record.get("output_frames"):
                raise FilmError(
                    f"frame_map rows {len(rows)} != output_frames "
                    f"{record.get('output_frames')}")
            if any(not _sha_ok(row.get("output_sha256")) for row in rows):
                raise FilmError("frame_map rows miss output_sha256")
            report["linkage"].append(
                {"link": "recipe:frame_map_rows", "ok": True,
                 "detail": f"{len(rows)} canonical rows"})
        except FilmError as e:
            problems.append(_problem(SCHEMA_INVALID,
                                     f"frame_map.jsonl: {e}",
                                     path="frame_map.jsonl"))

    # clean/subbed sequence roots are recorded; deep recomputes them
    sequences = record.get("sequences") or {}
    for role, key in (("clean", "clean_sequence_root"),
                      ("subbed", "subbed_sequence_root")):
        root = sequences.get(key)
        ok = _sha_ok(root)
        report["linkage"].append(
            {"link": f"{role}:{key}", "ok": ok,
             "detail": "sealed root recorded" if ok
                      else f"{key} missing or malformed"})
        if not ok:
            problems.append(_problem(
                SCHEMA_INVALID, f"sequences.{key} is not a sealed sha256"))

    stored = sequences.get("delivery_dir", "final_frames")
    try:
        from .animation_compiler import _check_frame_sequence
        _check_frame_sequence(build / stored, record["output_frames"])
        report["linkage"].append(
            {"link": "subbed:delivery_sequence", "ok": True,
             "detail": f"{stored}/F_000001..F_{record['output_frames']:06d} "
                       "contiguous"})
    except FilmError as e:
        problems.append(_problem(MISSING_ASSET, str(e), path=stored))

    subtitles = record.get("subtitles") or {}
    _hash_link(report, problems, build, "subbed:lyrics.ass", "lyrics.ass",
               subtitles.get("ass_sha256"))
    _hash_link(report, problems, build, "subbed:lyrics.srt", "lyrics.srt",
               subtitles.get("srt_sha256"))
    lyr = record.get("lyrics") or {}
    _hash_link(report, problems, build, "subbed:lyrics_source",
               "lyrics_source.txt", lyr.get("source_sha256"))
    timed = build / "lyrics_timed.json"
    if lyr.get("timing_sha256"):
        try:
            from .lyrics import timing_fingerprint
            got = timing_fingerprint(read(timed))
            ok = got == lyr["timing_sha256"]
            report["linkage"].append(
                {"link": "subbed:lyrics_timing", "ok": ok,
                 "detail": "cue timing fingerprint "
                           + ("matches" if ok else "differs")})
            if not ok:
                problems.append(_problem(
                    HASH_MISMATCH, "lyrics_timed.json no longer matches "
                    "the sealed timing fingerprint",
                    path="lyrics_timed.json"))
        except FilmError as e:
            problems.append(_problem(SCHEMA_INVALID, str(e),
                                     path="lyrics_timed.json"))

    # encode: artifact hashes + driver/profile contract
    outputs = record.get("outputs") or {}
    encoding = record.get("encoding") or {}
    for role in ("clean", "subbed"):
        out = outputs.get(role) or {}
        _hash_link(report, problems, build, f"encode:{role}_artifact",
                   out.get("file") or "", out.get("sha256"))
        enc = encoding.get(role) or {}
        ok = _sha_ok(enc.get("encode_digest"))
        report["linkage"].append(
            {"link": f"encode:{role}_digest", "ok": ok,
             "detail": "encode_digest recorded" if ok
                      else "encode_digest missing"})
        if not ok:
            problems.append(_problem(
                SCHEMA_INVALID, f"encoding.{role}.encode_digest missing"))
    driver = encoding.get("driver")
    from .animation_schema import ENCODE_DRIVERS
    if driver not in ENCODE_DRIVERS:
        problems.append(_problem(
            UNSUPPORTED_PROFILE,
            f"encode driver {driver!r} is not a contracted driver"))

    profile = record.get("storage_profile")
    if profile == "LOCAL_FULL":
        report["linkage"].append(
            {"link": "storage_profile", "ok": True,
             "detail": "LOCAL_FULL — independent copies, offline replay"})
    elif profile == "DRIVE_BOUNDED":
        problems.append(_problem(
            ARCHIVE_UNAVAILABLE,
            "DRIVE_BOUNDED replay needs its pinned archive objects; this "
            "local diagnosis was given no archive backend — the seal's "
            "bytes are kept, no substitute is fabricated"))
    else:
        problems.append(_problem(
            UNSUPPORTED_PROFILE,
            f"storage_profile {profile!r} is not a contracted profile"))

    if deep and rows is not None and document is not None \
            and registry is not None:
        _deep_recompose(build, record, document, registry, rows,
                        report, problems)


def _deep_recompose(build, record, document, registry, rows, report,
                    problems):
    """Recompute the clean/subbed sequence roots from archived bytes.

    The same deterministic compose `replay_local_full` runs — member
    decode, canvas fit, pairwise crossfade — so a drifted recipe or member
    surfaces here as a root mismatch, not as a wrong MP4 later.
    """
    from .animation_compiler import _compose_row
    from .media_verify import frame_pixel_sha256, image_pixel_sha256, \
        sequence_root
    snapshot = build / "snapshot"
    member_files = {}
    for entry in document["entries"]:
        pin = registry["assignments"].get(entry["shot_id"])
        if pin is None:
            problems.append(_problem(
                MISSING_ASSET, f"archive has no assignment for "
                               f"{entry['shot_id']}"))
            return
        asset = registry["assets"][pin["asset_id"]]["revisions"][
            str(pin["revision"])]
        member_files[entry["instance_id"]] = {
            f["frame_index"]: safe_path(snapshot, f["relative_name"])
            for f in asset["files"]}
    fmt = record["format"]
    try:
        digests = [image_pixel_sha256(
            _compose_row(row, member_files, fmt["width"], fmt["height"]))
            for row in rows]
        root = sequence_root("clean", digests)
        ok = root == (record.get("sequences") or {}).get(
            "clean_sequence_root")
        report["linkage"].append(
            {"link": "clean:recomposed_root", "ok": ok,
             "detail": "recomposed clean root "
                       + ("equals" if ok else "DIFFERS from")
                       + " the sealed clean_sequence_root"})
        if not ok:
            problems.append(_problem(
                HASH_MISMATCH, "recomposing the archived members does not "
                "reproduce the sealed clean_sequence_root"))
        stored = record["sequences"].get("delivery_dir", "final_frames")
        subbed_root = sequence_root(
            "subbed", [frame_pixel_sha256(build / stored / row_file_name(r))
                       for r in range(record["output_frames"])])
        ok = subbed_root == record["sequences"].get("subbed_sequence_root")
        report["linkage"].append(
            {"link": "subbed:stored_pixel_root", "ok": ok,
             "detail": "stored delivery pixels "
                       + ("equal" if ok else "DIFFER from")
                       + " the sealed subbed_sequence_root"})
        if not ok:
            problems.append(_problem(
                HASH_MISMATCH, "stored delivery frames do not decode to "
                "the sealed subbed_sequence_root"))
    except FilmError as e:
        problems.append(_problem(SCHEMA_INVALID,
                                 f"deep recompose failed: {e}"))


def row_file_name(index):
    from .frame_clock import frame_filename
    return frame_filename(index)


def _rebuild(build, record, kind, problems):
    """Compare the seal to the live project's current inputs.

    A rebuild allocates a new B#### and never rewrites or *becomes* this
    build; changed input layers mean the new build is a NEW_RESULT.
    """
    project = build.parent.parent if _BUILD_ID.fullmatch(build.name) \
        and build.parent.name == "builds" else None
    if project is None or not (project / "project.yaml").is_file():
        return {"state": REBUILD_UNKNOWN, "layers": {},
                "note": "no live project at <build>/../.. — replay-only "
                        "diagnosis"}
    layers = {}
    try:
        config = read(project / "project.yaml")
    except FilmError as e:
        return {"state": REBUILD_UNKNOWN, "layers": {},
                "project": str(project), "note": str(e)}
    from .core import production_profile
    profile = production_profile(config)
    audio_live = (config.get("audio") or {}).get("sha256")
    audio_sealed = (record.get("audio") or {}).get("sha256")
    layers["audio"] = _layer(audio_live == audio_sealed,
                             f"live master {audio_live} vs sealed "
                             f"{audio_sealed}")
    layers["format"] = _layer(config.get("format") == record.get("format"),
                              "output format comparison")

    if kind == "BUILD_2":
        if profile != "FRAME_ANIMATION_V1":
            return {"state": REBUILD_IMPOSSIBLE, "project": str(project),
                    "layers": layers,
                    "note": f"project profile is {profile}; the "
                            "FRAME_ANIMATION_V1 rebuild path is closed"}
        try:
            from .animation_review import edit_digest
            from .animation_schema import load_animation_timeline
            live_edit = edit_digest(load_animation_timeline(project))
            layers["edit"] = _layer(live_edit == record.get("edit_digest"),
                                    f"live edit {live_edit[:16]}… vs sealed")
        except FilmError as e:
            layers["edit"] = {"state": "MISSING", "detail": str(e)}
        try:
            from .animation_assets import load_registry
            registry = load_registry(project)
            shots = {}
            for entry in record.get("entries") or []:
                pin = registry["assignments"].get(entry["shot_id"])
                want = {"asset_id": entry.get("asset_id"),
                        "revision": entry.get("revision"),
                        "content_sha256": entry.get("content_sha256")}
                state = ("UNCHANGED" if pin is not None and all(
                    pin.get(k) == v for k, v in want.items()
                    if v is not None)
                    else "MISSING" if pin is None else "CHANGED")
                shots[entry["shot_id"]] = state
            changed = [s for s, v in shots.items() if v != "UNCHANGED"]
            layers["sources"] = {
                "state": "UNCHANGED" if not changed else
                         ("MISSING" if any(shots[s] == "MISSING"
                                           for s in changed)
                          else "CHANGED"),
                "detail": f"{len(shots) - len(changed)} of {len(shots)} "
                          f"shot pins unchanged; changed: {changed}",
                "shots": shots}
        except FilmError as e:
            layers["sources"] = {"state": "MISSING", "detail": str(e)}
        lyr = record.get("lyrics") or {}
        try:
            from .lyrics import timing_fingerprint
            live_timing = timing_fingerprint(
                read(project / "lyrics/lyrics_timed.json"))
            layers["lyrics"] = _layer(
                live_timing == lyr.get("timing_sha256"),
                "live cue timing fingerprint vs sealed")
        except FilmError as e:
            layers["lyrics"] = {"state": "MISSING", "detail": str(e)}
    else:
        if profile != "LEGACY_MV":
            return {"state": REBUILD_IMPOSSIBLE, "project": str(project),
                    "layers": layers,
                    "note": "project was converted to FRAME_ANIMATION_V1; "
                            "the legacy ms rebuild path is closed"}
        for name in ("manifest/shots.json", "manifest/sequence.json"):
            sealed = build / "snapshot" / name
            live = project / name
            if not sealed.is_file() or not live.is_file():
                layers[name] = {"state": "MISSING",
                                "detail": "manifest missing on one side"}
            else:
                layers[name] = _layer(
                    digest(live) == digest(sealed),
                    "live manifest bytes vs sealed snapshot")
        lyr = record.get("lyrics") or {}
        timed_live, timed_sealed = (project / "lyrics/lyrics_timed.json",
                                    build / "lyrics_timed.json")
        if lyr.get("timing_sha256") and timed_live.is_file() \
                and timed_sealed.is_file():
            layers["lyrics"] = _layer(
                digest(timed_live) == digest(timed_sealed),
                "live lyrics_timed.json bytes vs sealed copy")

    states = {v["state"] for v in layers.values()}
    if "MISSING" in states:
        state = REBUILD_IMPOSSIBLE
    elif "CHANGED" in states:
        state = REBUILD_NEW_RESULT
    elif states == {"UNCHANGED"} or not states:
        state = REBUILD_SAME_INPUTS
    else:
        state = REBUILD_UNKNOWN
    return {"state": state, "project": str(project), "layers": layers,
            "note": "a rebuild always allocates a new build id — when any "
                    "layer differs its outputs are a NEW_RESULT and are "
                    "never reported as this build"}


def _layer(same, detail):
    return {"state": "UNCHANGED" if same else "CHANGED", "detail": detail}


def diagnose_build(build_dir, *, current_toolchain=None, deep=False,
                   compare_project=True):
    """Diagnose why a sealed build can or cannot be replayed/rebuilt."""
    build = Path(build_dir).resolve()
    problems = []
    report = {"tool": "build-doctor", "build_dir": str(build),
              "generated_at": now(), "problems": problems,
              "linkage": [], "qualification": dict(_FACETS)}
    if not build.is_dir():
        raise FilmError(f"No build directory: {build}")
    try:
        record = _record(build)
    except FilmError as e:
        problems.append(_problem(SCHEMA_INVALID, str(e)))
        report.update(build_id=None, branch=BLOCKED)
        return report
    report["build_id"] = record.get("build_id")
    report["mode"] = record.get("mode")
    report["status"] = record.get("status")
    report["build_sha256"] = digest(build / "build.json")

    kind = _kind(record)
    report["build_kind"] = kind
    if kind is None:
        problems.append(_problem(
            UNSUPPORTED_PROFILE,
            "not a sealed Build 1/Build 2 record (draft previews and "
            "unknown document types do not replay)"))
    if record.get("status") != "COMPLETE":
        problems.append(_problem(
            INCOMPLETE_BUILD,
            f"build status is {record.get('status')!r}, not COMPLETE"))
    report["inventory"] = _inventory(build, record, problems)

    current = current_toolchain or globals()["current_toolchain"]()
    report["toolchain"] = _toolchain(record, current, problems)

    if kind == "BUILD_1":
        _linkage_build1(build, record, report, problems)
    elif kind == "BUILD_2":
        _linkage_build2(build, record, report, problems, deep)

    if compare_project and kind in ("BUILD_1", "BUILD_2"):
        report["rebuild"] = _rebuild(build, record, kind, problems)

    blocking = [p["class"] for p in problems if p["blocking"]]
    tool_diff = bool(report["toolchain"]["differences"])
    if blocking:
        branch = BLOCKED
    elif tool_diff:
        branch = REPRODUCE_NEW_RESULT
    else:
        branch = REPRODUCE_SAME
    report["reproduction"] = {
        "branch": branch,
        "blocking": sorted(set(blocking)),
        "note": ("REPRODUCE_SAME means the pinned inputs and toolchain "
                 "reproduce the sealed content — produced bytes are still "
                 "compared before being called identical. "
                 "REPRODUCE_NEW_RESULT means a replay/rebuild can run but "
                 "its differing outputs are new artifacts, never this "
                 "build again.")}
    return report


# --- archive diagnosis (bounded restore honesty) ---------------------------------

def restore_plan(index, *, members=None, range_supported=True,
                 whole_pack_cap=None, offline=False):
    """The explicit restore branch — bounded member ranges or nothing.

    `MEMBER_RANGES` plans exactly the selected members' byte ranges; a
    backend that cannot serve ranges does not turn this into a silent
    whole-source download: `WHOLE_PACK_VERIFIED` exists only when a cap was
    declared up front and the pinned pack fits inside it (or `offline`,
    which *is* a whole-object restore by definition and still needs the
    declared cap). Otherwise the plan is BLOCKED with RANGE_UNSUPPORTED or
    CAPACITY_BLOCKED.
    """
    index = validate_index(index)
    selected = ([member_for_frame(index, i) for i in members]
                if members and all(type(m) is int for m in members)
                else [m for m in index["members"]
                      if members is None or m["member_id"] in members])
    if not selected:
        raise FilmError("No pack members selected for the restore plan")
    member_bytes = sum(m["byte_length"] for m in selected)
    pack_length = index["pack_byte_length"]
    plan = {"member_ids": [m["member_id"] for m in selected],
            "byte_ranges": [[m["byte_offset"], m["byte_length"]]
                            for m in selected],
            "member_bytes": member_bytes,
            "pack_byte_length": pack_length,
            "offline_requested": bool(offline)}
    if not offline and range_supported:
        plan.update(branch=RESTORE_MEMBER_RANGES,
                    max_body_bytes=member_bytes, unrequested_bytes=0,
                    note="only the selected member ranges are fetched; "
                         "the rest of the pack is never read")
        return plan
    # A whole-object transfer is the only way — it must have been declared.
    if whole_pack_cap is None:
        plan.update(branch=RESTORE_BLOCKED, reason="RANGE_UNSUPPORTED",
                    note="the backend cannot serve ranges and no "
                         "whole-pack cap was declared — the unbounded "
                         "body is refused, not silently downloaded")
        return plan
    if pack_length > whole_pack_cap:
        plan.update(branch=RESTORE_BLOCKED, reason="CAPACITY_BLOCKED",
                    note=f"whole pack {pack_length} exceeds the declared "
                         f"cap {whole_pack_cap}")
        return plan
    plan.update(branch=RESTORE_WHOLE_PACK,
                whole_pack_cap_bytes=whole_pack_cap,
                note="explicit whole-pack branch: the full object is "
                     "downloaded inside the declared cap and verified "
                     "against pack_sha256")
    return plan


def diagnose_archive(document, backend, *, members=None, whole_pack_cap=None,
                     verify_members=True, sleep_fn=None):
    """Diagnose a sealed storage_archive + FAV1 pack against a backend."""
    problems = []
    report = {"tool": "archive-doctor", "generated_at": now(),
              "problems": problems, "qualification": dict(_FACETS)}
    try:
        doc = validate_archive(document)
    except FilmError as e:
        problems.append(_problem(SCHEMA_INVALID,
                                 f"storage_archive manifest: {e}"))
        report.update(branch=BLOCKED)
        return report
    report["archive_id"] = doc["archive_id"]
    report["storage_profile"] = doc["storage_profile"]

    objects = []
    for obj in doc["objects"]:
        entry = {"object_id": obj["object_id"], "kind": obj["kind"]}
        try:
            info = backend.object_info(obj["object_id"])
            entry["present"] = True
            entry["byte_length_match"] = \
                info.get("byte_length") == obj["byte_length"]
            if not entry["byte_length_match"]:
                problems.append(_problem(
                    HASH_MISMATCH, f"object {obj['object_id']} stored "
                                   "length differs from the manifest"))
        except FilmError as e:
            entry.update(present=False, detail=str(e))
            problems.append(_problem(
                MISSING_ASSET, f"archive object {obj['object_id']} "
                               f"unavailable: {e}"))
        objects.append(entry)
    report["objects"] = objects

    pack = doc.get("pack")
    if pack is None:
        problems.append(_problem(SCHEMA_INVALID,
                                 "the manifest holds no pack pin"))
        report.update(branch=BLOCKED)
        return report

    try:
        index = fetch_index(backend, pack["index_object_id"],
                            pack["index_sha256"],
                            retry=doc["transport"]["retry"],
                            sleep_fn=sleep_fn or (lambda ms: None))
    except FilmError as e:
        problems.append(_problem(INDEX_FAULT,
                                 f"pinned pack_index unreadable: {e}"))
        report.update(branch=BLOCKED)
        return report
    report["index"] = {"member_count": index["member_count"],
                       "frame_coverage": index["frame_coverage"],
                       "pack_sha256": index["pack_sha256"],
                       "pack_byte_length": index["pack_byte_length"]}

    try:
        plan = restore_plan(
            index, members=members,
            range_supported=getattr(backend, "range_supported", True),
            whole_pack_cap=whole_pack_cap)
    except FilmError as e:
        problems.append(_problem(INDEX_FAULT, str(e)))
        report.update(branch=BLOCKED)
        return report
    report["restore"] = plan

    member_report = []
    if verify_members and plan["branch"] != RESTORE_BLOCKED:
        reader = PackReader(backend, pack["object_id"], index,
                            whole_pack_cap=whole_pack_cap,
                            retry=doc["transport"]["retry"],
                            sleep_fn=sleep_fn or (lambda ms: None))
        wanted = set(plan["member_ids"])
        try:
            if plan["branch"] == RESTORE_WHOLE_PACK:
                reader.verify_full_pack()
            for member in index["members"]:
                if member["member_id"] not in wanted:
                    continue
                entry = {"member_id": member["member_id"]}
                try:
                    reader.read_member(member["member_id"])
                    entry["state"] = "VERIFIED"
                except FilmError as e:
                    text = str(e)
                    cls = PACK_FAULT if "SHA-256" in text \
                        or "hash" in text.lower() else RANGE_FAULT
                    entry.update(state="FAULT", detail=text)
                    problems.append(_problem(cls, text,
                                             path=member["member_id"]))
                member_report.append(entry)
        except FilmError as e:
            problems.append(_problem(RANGE_FAULT, str(e)))
        fetched = reader.fetched_bytes
        report["transfer"] = {
            "verification_state": reader.state,
            "fetched_bytes": fetched,
            # The honesty invariant: a bounded restore fetched exactly the
            # member bytes it asked for; a whole-pack fetch is attributed
            # to the explicit WHOLE_PACK_VERIFIED branch only.
            "unrequested_bytes": max(
                0, fetched - (plan["pack_byte_length"]
                              if plan["branch"] == RESTORE_WHOLE_PACK
                              else plan["member_bytes"]))}
    report["members"] = member_report
    blocking = [p["class"] for p in problems if p["blocking"]]
    report["branch"] = BLOCKED if blocking else plan["branch"]
    report["reproduction"] = {"branch": report["branch"],
                              "blocking": sorted(set(blocking))}
    return report


# --- replay packet (no credentials, ever) ----------------------------------------

PACKET_TYPE = "replay_packet"
PACKET_FIELDS = {"document_type", "schema_version", "packet_id",
                 "created_at", "build", "inputs", "recipe", "toolchain",
                 "deliverables", "restore", "credentials", "reproduction",
                 "qualification", "notes"}
_PACKET_BUILD_FIELDS = {"build_id", "document_type", "build_schema",
                        "mode", "status", "storage_profile", "seal_sha256"}
_PACKET_INPUT_FIELDS = {"role", "name", "sha256", "byte_length"}
_PACKET_DELIVERABLE_FIELDS = {"file", "sha256", "expectation"}
_PACKET_RESTORE_FIELDS = {"mode", "member_ids", "byte_ranges",
                          "whole_pack_cap_bytes", "offline_ready", "note"}
_PACKET_CRED_FIELDS = {"embedded", "required"}
_PACKET_CRED_REQ_FIELDS = {"kind", "in_packet", "note"}
_PACKET_REPRO_FIELDS = {"branch", "blocking", "note"}
_FACET_FIELDS = {"node_state", "qualification_state", "acceptance_state",
                 "release_state"}
RESTORE_MODES = {"LOCAL_FILES", "MEMBER_RANGES", "WHOLE_PACK_VERIFIED",
                 "ARCHIVE_REQUIRED"}
EXPECTATIONS = {EXPECT_PRESERVED, EXPECT_RECOMPOSE, EXPECT_REENCODE}


def _no_secrets(value, where="replay_packet"):
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is str and key not in _SECRET_KEY_ALLOWED \
                    and _SECRET_KEY_RE.search(key):
                raise FilmError(
                    f"{where}: field {key!r} is shaped like a credential — "
                    "replay packets never carry tokens, keys or sessions")
            _no_secrets(child, where)
    elif isinstance(value, list):
        for child in value:
            _no_secrets(child, where)
    elif isinstance(value, str):
        for pattern in _SECRET_VALUE_RES:
            if pattern.search(value):
                raise FilmError(
                    f"{where}: a value is shaped like a credential — "
                    "secrets live in the user's secret store only")


def validate_replay_packet(document):
    """Structural + no-secrets contract of `replay_packet` 1."""
    check_document(document, PACKET_TYPE)
    if type(document) is not dict or set(document) != PACKET_FIELDS:
        raise FilmError("replay_packet must hold exactly the contracted "
                        "fields")
    canon_bytes(document)
    if type(document["packet_id"]) is not str \
            or not document["packet_id"]:
        raise FilmError("packet_id must be a non-empty string")
    if type(document["created_at"]) is not str:
        raise FilmError("created_at must be an ISO timestamp string")
    build = document["build"]
    if type(build) is not dict or set(build) != _PACKET_BUILD_FIELDS:
        raise FilmError("replay_packet.build fields are fixed")
    if type(build["build_id"]) is not str or not build["build_id"]:
        raise FilmError("build.build_id must be a non-empty string")
    _sha_or_none(build["seal_sha256"], "build.seal_sha256")
    inputs = document["inputs"]
    if type(inputs) is not list or not inputs:
        raise FilmError("replay_packet.inputs must be a non-empty list")
    seen = set()
    for position, item in enumerate(inputs):
        if type(item) is not dict or set(item) != _PACKET_INPUT_FIELDS:
            raise FilmError(f"replay_packet.inputs[{position}] fields are "
                            "fixed")
        if type(item["role"]) is not str or not item["role"] \
                or type(item["name"]) is not str or not item["name"]:
            raise FilmError("input role/name must be non-empty strings")
        if item["name"] in seen:
            raise FilmError(f"duplicate input name {item['name']}")
        seen.add(item["name"])
        _sha_or_none(item["sha256"], "inputs.sha256")
        if type(item["byte_length"]) is not int or item["byte_length"] < 0:
            raise FilmError("input byte_length must be an integer >= 0")
    if type(document["recipe"]) is not dict:
        raise FilmError("replay_packet.recipe must be an object")
    toolchain = document["toolchain"]
    if toolchain is not None and (type(toolchain) is not dict
                                  or set(toolchain) != {"python", "ffmpeg",
                                                        "compiler"}):
        raise FilmError("replay_packet.toolchain is the sealed "
                        "{python, ffmpeg, compiler} triple or null")
    deliverables = document["deliverables"]
    if type(deliverables) is not list:
        raise FilmError("deliverables must be a list")
    for item in deliverables:
        if type(item) is not dict or set(item) != _PACKET_DELIVERABLE_FIELDS:
            raise FilmError("deliverable fields are fixed")
        if item["expectation"] not in EXPECTATIONS:
            raise FilmError(f"unknown deliverable expectation "
                            f"{item['expectation']}")
        _sha_or_none(item["sha256"], "deliverables.sha256")
    restore = document["restore"]
    if type(restore) is not dict or set(restore) != _PACKET_RESTORE_FIELDS:
        raise FilmError("replay_packet.restore fields are fixed")
    if restore["mode"] not in RESTORE_MODES:
        raise FilmError(f"unknown restore mode {restore['mode']}")
    if restore["member_ids"] is not None \
            and type(restore["member_ids"]) is not list:
        raise FilmError("restore.member_ids is a list or null")
    if restore["byte_ranges"] is not None and (
            type(restore["byte_ranges"]) is not list or any(
                type(r) is not list or len(r) != 2
                or any(type(v) is not int or v < 0 for v in r)
                for r in restore["byte_ranges"])):
        raise FilmError("restore.byte_ranges are [offset, length) pairs")
    if restore["whole_pack_cap_bytes"] is not None and (
            type(restore["whole_pack_cap_bytes"]) is not int
            or restore["whole_pack_cap_bytes"] < 1):
        raise FilmError("whole_pack_cap_bytes is a positive int or null")
    if type(restore["offline_ready"]) is not bool:
        raise FilmError("restore.offline_ready must be a boolean")
    credentials = document["credentials"]
    if type(credentials) is not dict or set(credentials) != _PACKET_CRED_FIELDS:
        raise FilmError("replay_packet.credentials fields are fixed")
    if credentials["embedded"] != "NEVER":
        raise FilmError("replay_packet.credentials.embedded is always "
                        "'NEVER' — packets never carry tokens")
    if type(credentials["required"]) is not list:
        raise FilmError("credentials.required must be a list")
    for req in credentials["required"]:
        if type(req) is not dict or set(req) != _PACKET_CRED_REQ_FIELDS \
                or req["in_packet"] is not False:
            raise FilmError("credential requirements are {kind, "
                            "in_packet: false, note} — a grant's value is "
                            "never embedded")
    repro = document["reproduction"]
    if type(repro) is not dict or set(repro) != _PACKET_REPRO_FIELDS:
        raise FilmError("replay_packet.reproduction fields are fixed")
    if repro["branch"] not in (REPRODUCE_SAME, REPRODUCE_NEW_RESULT,
                               BLOCKED):
        raise FilmError(f"unknown reproduction branch {repro['branch']}")
    if type(repro["blocking"]) is not list:
        raise FilmError("reproduction.blocking must be a list")
    if type(document["qualification"]) is not dict \
            or set(document["qualification"]) != _FACET_FIELDS:
        raise FilmError("qualification must carry the four facets")
    if type(document["notes"]) is not list:
        raise FilmError("notes must be a list")
    _no_secrets(document)
    return document


def _sha_or_none(value, what):
    if value is not None and not _sha_ok(value):
        raise FilmError(f"{what} must be a lowercase sha256 or null")


def _packet_input(role, name, path):
    data = Path(path)
    return {"role": role, "name": name, "sha256": digest(data),
            "byte_length": data.stat().st_size}


def build_replay_packet(build_dir, *, report=None):
    """Assemble the credential-free replay packet for a sealed build."""
    build = Path(build_dir).resolve()
    report = report or diagnose_build(build)
    record = _record(build)
    kind = _kind(record)
    if kind is None:
        raise FilmError("replay packets apply to sealed Build 1/Build 2 "
                        "records only")
    inputs, deliverables = [], []

    def _add(role, relative, base=build):
        path = safe_path(base, relative)
        if path.is_file():
            inputs.append(_packet_input(role, relative, path))
            return path
        return None

    if kind == "BUILD_2":
        _add("timeline_edit", "snapshot/timeline/edit.json")
        _add("asset_registry", "snapshot/manifest/animation_assets.json")
        _add("frame_map", "frame_map.jsonl")
        for name in ("lyrics_source.txt", "lyrics_timed.json"):
            _add("lyrics", name)
        for f in sorted((build / "subtitle_fonts").glob("*")
                        if (build / "subtitle_fonts").is_dir() else []):
            _add("subtitle_font", f"subtitle_fonts/{f.name}")
        audio = record.get("audio") or {}
        _add("master_audio", audio.get("path") or "")
        _add("shots_manifest", "snapshot/manifest/shots.json")
        try:
            registry = read_canon(
                build / "snapshot/manifest/animation_assets.json")
            for entry in record.get("entries") or []:
                pin = registry["assignments"].get(entry["shot_id"])
                if pin is None:
                    continue
                revision = registry["assets"][pin["asset_id"]][
                    "revisions"][str(pin["revision"])]
                for f in revision["files"]:
                    _add("source_member",
                         f"snapshot/{f['relative_name']}")
        except FilmError:
            pass  # registry faults are already classified by the doctor
        seq = record.get("sequences") or {}
        enc = record.get("encoding") or {}
        recipe = {
            "edit_digest": record.get("edit_digest"),
            "frame_map_sha256": record.get("frame_map_sha256"),
            "clean_sequence_root": seq.get("clean_sequence_root"),
            "subbed_sequence_root": seq.get("subbed_sequence_root"),
            "subtitle_recipe_digest":
                (record.get("subtitles") or {}).get("recipe_digest"),
            "delivery_profile": enc.get("delivery_profile"),
            "driver": enc.get("driver"),
            "encode_digests": {role: (enc.get(role) or {})
                               .get("encode_digest")
                               for role in ("clean", "subbed")},
            "output_frames": record.get("output_frames"),
            "format": record.get("format")}
        for frame in sorted((build / "final_frames").glob("F_*.png")):
            _add("delivery_frame", f"final_frames/{frame.name}")
        outputs = record.get("outputs") or {}
        for role in ("clean", "subbed"):
            out = outputs.get(role) or {}
            if out.get("file"):
                deliverables.append({
                    "file": out["file"], "sha256": out.get("sha256"),
                    "expectation": EXPECT_REENCODE})
        for name in ("lyrics.ass", "lyrics.srt", "lyrics_source.txt",
                     "lyrics_timed.json", "subtitle_report.json",
                     "frame_map.jsonl"):
            inv = (record.get("files") or {}).get(name)
            if inv:
                deliverables.append({"file": name, "sha256": inv,
                                     "expectation": EXPECT_PRESERVED})
        restore = {"mode": "LOCAL_FILES", "member_ids": None,
                   "byte_ranges": None, "whole_pack_cap_bytes": None,
                   "offline_ready": True,
                   "note": "LOCAL_FULL — every input is an independent "
                           "copy inside the build directory"}
        credentials_required = []
        if record.get("storage_profile") == "DRIVE_BOUNDED":
            restore.update(mode="ARCHIVE_REQUIRED", offline_ready=False,
                           note="DRIVE_BOUNDED replay needs the pinned "
                                "archive objects; no offline copy is "
                                "fabricated")
            credentials_required.append({
                "kind": "ARCHIVE_READ",
                "in_packet": False,
                "note": "the user's archive read grant at replay time; "
                        "never embedded in this packet"})
    else:
        audio = record.get("audio") or {}
        _add("master_audio", audio.get("path") or "")
        for shot in record.get("shots") or []:
            _add("normalized_clip", shot.get("clip_path") or "")
            _add("selected_source", shot.get("source_path") or "")
        for name in ("lyrics.ass", "lyrics.srt", "lyrics_source.txt",
                     "lyrics_timed.json", "subtitle_report.json"):
            _add("subtitle_input", name)
        for f in sorted((build / "subtitle_fonts").glob("*")
                        if (build / "subtitle_fonts").is_dir() else []):
            _add("subtitle_font", f"subtitle_fonts/{f.name}")
        recipe = {"compiler": "0.3.0", "kind": "BUILD_1_CONCAT",
                  "duration_ms": record.get("duration_ms"),
                  "format": record.get("format")}
        for name in ("MASTER_CLEAN.mp4", "MASTER_SUBBED.mp4",
                     "lyrics.ass", "lyrics.srt"):
            inv = (record.get("files") or {}).get(name)
            if inv:
                deliverables.append({
                    "file": name, "sha256": inv,
                    "expectation": EXPECT_REENCODE if name.endswith(".mp4")
                    else EXPECT_PRESERVED})
        restore = {"mode": "LOCAL_FILES", "member_ids": None,
                   "byte_ranges": None, "whole_pack_cap_bytes": None,
                   "offline_ready": True,
                   "note": "Build 1 replay reassembles the captured "
                           "normalized clips, master and ASS"}
        credentials_required = []

    repro = report.get("reproduction") or {"branch": BLOCKED,
                                           "blocking": [],
                                           "note": "no diagnosis"}
    packet = {
        "document_type": PACKET_TYPE, "schema_version": 1,
        "packet_id": "rpkt-" + hashlib.sha256(
            canon_bytes({"build_id": record.get("build_id"),
                         "seal": report.get("build_sha256"),
                         "inputs": [i["sha256"] for i in inputs]})
        ).hexdigest()[:16],
        "created_at": now(),
        "build": {"build_id": record.get("build_id"),
                  "document_type": record.get("document_type"),
                  "build_schema": record.get("schema_version"),
                  "mode": record.get("mode"),
                  "status": record.get("status"),
                  "storage_profile": record.get("storage_profile"),
                  "seal_sha256": report.get("build_sha256")},
        "inputs": inputs,
        "recipe": recipe,
        "toolchain": record.get("toolchain"),
        "deliverables": deliverables,
        "restore": restore,
        "credentials": {"embedded": "NEVER",
                        "required": credentials_required},
        "reproduction": {"branch": repro["branch"],
                         "blocking": list(repro.get("blocking") or []),
                         "note": repro.get("note")},
        "qualification": dict(_FACETS),
        "notes": [
            "this packet pins content digests only — locators, sessions "
            "and credentials are never included",
            "differing bytes produced by a replay/rebuild are new "
            "artifacts and are never reported as this build",
            "synthetic/local development artifact — not artwork approval, "
            "qualification or release evidence"]}
    return validate_replay_packet(packet)


def write_replay_packet(path, document):
    validate_replay_packet(document)
    write_canon(path, document)
    return Path(path)


def read_replay_packet(path):
    return validate_replay_packet(read_canon(path))


# --- replay output comparison ------------------------------------------------------

def _deliverable_names(record):
    names = set()
    outputs = record.get("outputs") or {}
    for role in ("clean", "subbed"):
        out = outputs.get(role) or {}
        if out.get("file"):
            names.add(out["file"])
    if not names:
        names.update(n for n in ("MASTER_CLEAN.mp4", "MASTER_SUBBED.mp4")
                     if n in (record.get("files") or {}))
    for name in ("lyrics.ass", "lyrics.srt", "frame_map.jsonl",
                 "subtitle_report.json"):
        if name in (record.get("files") or {}):
            names.add(name)
    return names


def compare_replay_output(build_dir, produced_dir):
    """Compare produced replay files against the sealed inventory.

    The honesty rule: bytes that differ from the sealed hash are a
    `NEW_ARTIFACT` and the produced directory is never reported as the
    same build — `same_build_bytes` stays false and the label says so.
    """
    build, out = Path(build_dir).resolve(), Path(produced_dir).resolve()
    record = _record(build)
    inventory = record.get("files") or {}
    if not out.is_dir():
        raise FilmError(f"No produced directory: {out}")
    files = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        rel = str(path.relative_to(out))
        expected = inventory.get(rel)
        produced = digest(path)
        if expected is None:
            verdict = ARTIFACT_EXTRA
        elif produced == expected:
            verdict = ARTIFACT_IDENTICAL
        else:
            verdict = ARTIFACT_NEW
        files.append({"file": rel, "expected_sha256": expected,
                      "produced_sha256": produced, "verdict": verdict})
    missing = sorted(n for n in _deliverable_names(record)
                     if not (out / n).is_file())
    identical = sum(1 for f in files if f["verdict"] == ARTIFACT_IDENTICAL)
    new = [f["file"] for f in files if f["verdict"] == ARTIFACT_NEW]
    extra = [f["file"] for f in files if f["verdict"] == ARTIFACT_EXTRA]
    if missing:
        result = "INCOMPLETE"
    elif new:
        result = "REPRODUCED_NEW_ARTIFACT"
    else:
        result = "REPRODUCED_IDENTICAL"
    build_id = record.get("build_id")
    return {
        "sealed_build": build_id,
        "build_dir": str(build),
        "produced_dir": str(out),
        "files": files,
        "not_produced": missing,
        "summary": {"identical": identical, "new_artifacts": len(new),
                    "extra": len(extra), "not_produced": len(missing)},
        "extra_files": extra,
        "same_build_bytes": result == "REPRODUCED_IDENTICAL",
        "result": result,
        "label": (f"{build_id} replay — identical bytes"
                  if result == "REPRODUCED_IDENTICAL" else
                  f"{build_id} replay — produced files are NEW "
                  f"ARTIFACTS, not the sealed bytes: {new}"
                  if result == "REPRODUCED_NEW_ARTIFACT" else
                  f"{build_id} replay — incomplete; expected "
                  f"deliverables not produced: {missing}"),
        "note": "a produced file whose hash differs is a new artifact; "
                "it is never the sealed build again",
        "qualification": dict(_FACETS)}


# --- CLI adapters ------------------------------------------------------------------

def build_doctor_command(build_dir, *, deep=False, compare_project=True):
    return diagnose_build(build_dir, deep=deep,
                          compare_project=compare_project)


def archive_doctor_command(manifest, *, root=None, members=None,
                           whole_pack_cap=None):
    from .storage_backends.local import LocalArchiveBackend
    doc = read_canon(manifest)
    check_document(doc, "storage_archive")
    path = Path(manifest)
    if root is None:
        if path.parent.name == "manifests":
            root = path.parent.parent
        else:
            raise FilmError("Pass --root: the manifest is not under a "
                            "'manifests/' directory")
    backend = LocalArchiveBackend(root)
    return diagnose_archive(
        doc, backend,
        members=[m.strip() for m in members.split(",") if m.strip()]
        if members else None,
        whole_pack_cap=whole_pack_cap)


def replay_packet_command(build_dir, output):
    packet = build_replay_packet(build_dir)
    path = write_replay_packet(output, packet)
    return {"packet": str(path), "packet_id": packet["packet_id"],
            "build_id": packet["build"]["build_id"],
            "branch": packet["reproduction"]["branch"],
            "inputs": len(packet["inputs"]),
            "credentials": packet["credentials"]}


def replay_check_command(build_dir, produced_dir):
    return compare_replay_output(build_dir, produced_dir)
