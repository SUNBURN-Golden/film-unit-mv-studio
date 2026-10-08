"""film-delivery-package — hand off a picture with proof of what it is.

A delivery bundle is a new directory of copied bytes plus a canonical
manifest. It packages the sealed clean and subbed masters, a thumbnail,
the subtitle exports, a QC projection and the current review scope. It
checks the delivery profile, that sealed originals still match the hashes
the build recorded, and that a later download re-hashes to the manifest.

Three gates stay independent of the file name:

- ``FINAL_CANDIDATE_READY`` comes only from a sealed Build 2 record whose
  inventory still verifies;
- director adoption is a current ``FINAL_FILM`` protocol record whose
  bound hashes equal the included bytes;
- external publication stays ``NOT_AUTHORIZED``.

A name containing ``Final`` is not an input to any of those. Preview,
fake and source deliveries are not a work Final and not a service
qualification. Artwork acceptance stays ``PENDING``. Secrets, home
paths and email addresses are refused rather than copied. Existing
reviews and LOCKs are not inherited onto a different build. The live
project's audio, lyric source and cue files are only read.
"""
from pathlib import Path
import re
import shutil

from PIL import Image

from .animation_review import film_review_status
from .animation_schema import (canon_bytes, check_document, read_canon,
                               require_animation_profile, write_canon)
from .builds import verify_build
from .core import FilmError, digest, now, project_mutex, read, safe_path
from .frame_clock import frame_filename
from .quality_diagnostics import load_report

BUNDLE_TYPE = "delivery_bundle"
SCOPE_TYPE = "delivery_review_scope"
SCHEMA = 1

KNOWN_PROFILES = frozenset({"MV_H264_AAC_V1"})
WORK_CONTRACT = {"fps": {"num": 24, "den": 1}, "seconds": 240, "frames": 5760}

_BUILD_ID = re.compile(r"B[0-9]{4,}")
_SHA = re.compile(r"[0-9a-f]{64}")

_MEDIA = {"clean_master": "MASTER_CLEAN.mp4",
          "subbed_master": "MASTER_SUBBED.mp4",
          "draft_preview": "DRAFT_PREVIEW.mp4"}
_SUBS = {"subtitle_ass": "lyrics.ass", "subtitle_srt": "lyrics.srt"}

_SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"\bya29\.[0-9a-zA-Z_-]+"),
    re.compile(r"\bAIza[0-9a-zA-Z_-]{20,}"),
    re.compile(r"\b(?:xox[bapors]|ghp|gho|sk|pat)[-_][0-9a-zA-Z_-]{8,}"),
]
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_HOME_RE = re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\Users\\|\\Users\\)")
_TEXT_SUFFIXES = {".json", ".ass", ".srt", ".txt", ".md", ".sha256"}

_FACETS = {"node_state": "IN_PROGRESS",
           "qualification_state": "UNQUALIFIED",
           "acceptance_state": "PENDING",
           "release_state": "NOT_AUTHORIZED"}

_NOTES = (
    "Preview, fake and source delivery are not a work Final and not a "
    "service qualification.",
    "A file name containing Final does not change approval, adoption or "
    "release.",
    "Artwork acceptance is PENDING and external publication is "
    "NOT_AUTHORIZED in this environment.",
    "Existing reviews and LOCKs are not inherited onto this bundle.",
)

_BUNDLE_KEYS = {
    "document_type", "schema_version", "bundle_id", "project_id",
    "build_id", "created_at", "delivery_profile", "profile_check",
    "profile_problems", "storage_profile", "delivery_complete",
    "approval_consistent", "members", "sources", "qc", "states",
    "facets", "work_contract", "delivery_clock", "meets_work_contract",
    "notes"}
_MEMBER_KEYS = {"role", "path", "sha256", "bytes", "scope_class"}
_SOURCE_KEYS = {"role", "preservation", "sealed_sha256", "live_sha256"}
_QC_KEYS = {"attached", "binding", "not_an_approval", "path",
            "defect_count", "candidate_count"}
_STATE_KEYS = {"preview", "final_candidate", "director_adoption",
               "artwork_acceptance", "external_publication",
               "filename_is_not_approval"}
_FACET_KEYS = {"node_state", "qualification_state", "acceptance_state",
               "release_state"}
_CLOCK_KEYS = {"fps", "frames", "frames_on_disk"}
_SCOPE_KEYS = {
    "document_type", "schema_version", "build_id", "film_review_state",
    "film_review_id", "deliverables", "scope_match", "compared",
    "locks_are_not_approval", "inherited_reviews"}
_DELIVERABLE_KEYS = {"deliverable", "sha256", "state", "review_id",
                     "governed", "reviewer_kind", "approval_id"}
_COMPARED_KEYS = {"role", "artifact_sha256", "scope_sha256", "match",
                  "state"}


def _build_id(build_id):
    if type(build_id) is not str or not _BUILD_ID.fullmatch(build_id):
        raise FilmError("build_id must be a B0001-style build directory name")
    return build_id


def _sha_or_none(value):
    return value if type(value) is str and _SHA.fullmatch(value) else None


def _scan(data, *, emails):
    text = data.decode("utf-8", errors="replace")
    for pattern in _SECRET_RES:
        if pattern.search(text):
            return "secret"
    if _HOME_RE.search(text):
        return "personal-path"
    if emails and _EMAIL_RE.search(text):
        return "email"
    return None


def _scan_file(path):
    data = Path(path).read_bytes()
    return _scan(data, emails=Path(path).suffix.lower() in _TEXT_SUFFIXES)


def _output_dir(project, build_dir, output):
    output = Path(output)
    if output.exists():
        raise FilmError(
            "delivery output already exists; choose a new directory")
    resolved = output.resolve()
    build_resolved = build_dir.resolve()
    project_resolved = Path(project).resolve()
    builds = (project_resolved / "builds").resolve()
    if resolved == build_resolved or build_resolved in resolved.parents \
            or resolved in build_resolved.parents:
        raise FilmError(
            "delivery bundle must be a new directory outside the sealed build")
    if resolved == builds or builds in resolved.parents \
            or resolved == project_resolved:
        raise FilmError(
            "delivery bundle must not replace the project or its builds/")
    return output


def _refuse_symlink(path, label):
    if Path(path).is_symlink():
        raise FilmError(f"refusing to bundle a symlink: {label}")


def _copy_member(source, destination, label):
    _refuse_symlink(source, label)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    kind = _scan_file(destination)
    if kind:
        raise FilmError(
            f"{label} contains a {kind} pattern and was not bundled")
    return destination


def _preview(record):
    if record.get("mode") in {"PREVIEW", "DRAFT"} or record.get("draft") is True:
        return "PREVIEW"
    return "NOT_PREVIEW"


def _candidate(folder, record, preview):
    if preview == "PREVIEW":
        return "NOT_A_CANDIDATE"
    if record.get("document_type") == "animation_build" \
            and record.get("schema_version") == 2 \
            and record.get("mode") == "FINAL_CANDIDATE" \
            and record.get("candidate_state") == "FINAL_CANDIDATE_READY" \
            and record.get("status") == "COMPLETE" \
            and verify_build(folder).get("valid"):
        return "FINAL_CANDIDATE_READY"
    return "NOT_A_CANDIDATE"


def _clock(record, folder):
    raw = (record.get("frame_clock") or {}).get("fps")
    fps = None
    if type(raw) is dict and type(raw.get("num")) is int \
            and type(raw.get("den")) is int and raw["den"] > 0:
        fps = {"num": raw["num"], "den": raw["den"]}
    frames = record.get("output_frames")
    if type(frames) is not int:
        frames = None
    on_disk = 0
    delivery = (record.get("sequences") or {}).get("delivery_dir")
    if type(delivery) is str and delivery and not Path(delivery).is_absolute():
        try:
            directory = safe_path(folder, delivery)
        except FilmError:
            directory = None
        if directory is not None and directory.is_dir():
            on_disk = sum(1 for _ in directory.glob("F_*.png"))
    return {"fps": fps, "frames": frames, "frames_on_disk": on_disk}


def _meets(clock):
    return clock["fps"] == WORK_CONTRACT["fps"] \
        and clock["frames"] == WORK_CONTRACT["frames"] \
        and clock["frames_on_disk"] == WORK_CONTRACT["frames"]


def _profile(record, folder):
    name = (record.get("encoding") or {}).get("delivery_profile")
    if type(name) is not str or not name.strip():
        return None, "UNSPECIFIED", []
    name = name.strip()
    problems = []
    outputs = record.get("outputs") or {}
    for role, filename in (("clean", "MASTER_CLEAN.mp4"),
                           ("subbed", "MASTER_SUBBED.mp4")):
        path = folder / filename
        if not path.is_file() or path.is_symlink():
            problems.append(f"missing {filename}")
            continue
        expected = (outputs.get(role) or {}).get("sha256")
        if expected != digest(path):
            problems.append(f"{filename} hash != outputs.{role}.sha256")
    if problems:
        return name, "HASH_MISMATCH", problems
    if name not in KNOWN_PROFILES:
        return name, "UNRECOGNIZED", []
    return name, "MATCH", []


def _preservation(sealed, live):
    if sealed is None and live is None:
        return "MISSING", None, None
    if sealed is None:
        return "NOT_SEALED", None, live
    if live is None:
        return "MISSING", sealed, None
    if sealed == live:
        return "PRESERVED", sealed, live
    return "DIVERGED", sealed, live


def _file_sha(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    return digest(path)


def _sources(project, folder, record):
    config = read(project / "project.yaml")
    audio_rel = (config.get("audio") or {}).get("path")
    live_audio = None
    if type(audio_rel) is str and audio_rel:
        try:
            live_audio = _file_sha(safe_path(project, audio_rel))
        except FilmError:
            live_audio = None
    sealed_audio_path = (record.get("audio") or {}).get("path")
    sealed_audio = None
    if type(sealed_audio_path) is str and sealed_audio_path:
        try:
            sealed_audio = _file_sha(safe_path(folder, sealed_audio_path))
        except FilmError:
            sealed_audio = None
    recorded_audio = _sha_or_none((record.get("audio") or {}).get("sha256"))
    audio_state, audio_sealed, audio_live = _preservation(
        recorded_audio, live_audio)
    if sealed_audio is not None and recorded_audio is not None \
            and sealed_audio != recorded_audio:
        audio_state = "DIVERGED"
    rows = [{"role": "audio", "preservation": audio_state,
             "sealed_sha256": audio_sealed if audio_sealed else recorded_audio,
             "live_sha256": audio_live}]
    lyric_rel = "input/lyrics.txt"
    try:
        live_lyrics = _file_sha(safe_path(project, lyric_rel))
    except FilmError:
        live_lyrics = None
    recorded_lyrics = _sha_or_none(
        (record.get("lyrics") or {}).get("source_sha256"))
    state, sealed, live = _preservation(recorded_lyrics, live_lyrics)
    rows.append({"role": "lyrics_source", "preservation": state,
                 "sealed_sha256": sealed, "live_sha256": live})
    subtitles = record.get("subtitles") or {}
    for role, filename, field in (
            ("cues_ass", "lyrics.ass", "ass_sha256"),
            ("cues_srt", "lyrics.srt", "srt_sha256")):
        recorded = _sha_or_none(subtitles.get(field))
        actual = _file_sha(folder / filename)
        cue_state, cue_sealed, cue_live = _preservation(recorded, actual)
        rows.append({"role": role, "preservation": cue_state,
                     "sealed_sha256": cue_sealed, "live_sha256": cue_live})
    return rows


def _reviews_for(project, build_id, deliverable):
    from .animation_review import load_reviews
    return [r for r in load_reviews(project)
            if r.get("scope") == "FINAL_FILM"
            and r.get("build_id") == build_id
            and r.get("deliverable") == deliverable]


def _scope_rows(project, folder, build_id):
    from .w00_gate import deliverable_status
    status = deliverable_status(project, build_id)
    film = film_review_status(project, build_id)
    deliverables = []
    compared = []
    role_for = {"MASTER_CLEAN.mp4": "clean_master",
                "MASTER_SUBBED.mp4": "subbed_master"}
    for name, row in status["deliverables"].items():
        reviews = _reviews_for(project, build_id, name)
        latest = reviews[-1] if reviews else None
        bound = _sha_or_none(latest["deliverable_sha256"]) if latest else None
        actual = row.get("deliverable_sha256")
        match = bool(latest) and row["state"] == "CURRENT" \
            and actual is not None and actual == bound
        deliverables.append({
            "deliverable": name,
            "sha256": actual,
            "state": row["state"],
            "review_id": row["review_id"],
            "governed": bool(row["governed"]),
            "reviewer_kind": row["reviewer_kind"],
            "approval_id": row["approval_id"]})
        compared.append({
            "role": role_for[name],
            "artifact_sha256": actual,
            "scope_sha256": bound,
            "match": match,
            "state": row["state"]})
    reviewed = [row for row in compared if row["scope_sha256"] is not None]
    if not reviewed:
        scope_match = "NO_CURRENT_SCOPE"
    elif all(row["match"] for row in reviewed):
        scope_match = "MATCH"
    else:
        scope_match = "MISMATCH"
    scope = {
        "document_type": SCOPE_TYPE,
        "schema_version": SCHEMA,
        "build_id": build_id,
        "film_review_state": film["state"],
        "film_review_id": film.get("review_id"),
        "deliverables": deliverables,
        "scope_match": scope_match,
        "compared": compared,
        "locks_are_not_approval": True,
        "inherited_reviews": False}
    return scope


def _states(preview, candidate, scope):
    subbed = next((row for row in scope["compared"]
                   if row["role"] == "subbed_master"), None)
    adopted = candidate == "FINAL_CANDIDATE_READY" and subbed is not None \
        and subbed["match"] and scope["scope_match"] == "MATCH"
    return {
        "preview": preview,
        "final_candidate": candidate,
        "director_adoption": "PROTOCOL_ADOPTED" if adopted else "NOT_ADOPTED",
        "artwork_acceptance": "PENDING",
        "external_publication": "NOT_AUTHORIZED",
        "filename_is_not_approval": True}


def _qc_projection(project, build_id, folder):
    report = load_report(project, build_id)
    if not report:
        return {"attached": False, "binding": "ABSENT",
                "not_an_approval": True, "path": None,
                "defect_count": 0, "candidate_count": 0}, None
    summary = report.get("summary") if type(report.get("summary")) is dict else {}
    findings = []
    for finding in report.get("findings") or []:
        if type(finding) is not dict:
            continue
        item = {key: finding.get(key) for key in
                ("id", "kind", "severity", "artifact")}
        if all(type(value) is str and value for value in item.values()):
            findings.append(item)
    artifacts = {}
    for name in ("MASTER_CLEAN.mp4", "MASTER_SUBBED.mp4"):
        block = (report.get("artifacts") or {}).get(name) or {}
        recorded = _sha_or_none(block.get("sha256"))
        if recorded:
            artifacts[name] = recorded
    projection = {
        "report_type": "quality_diagnostics",
        "not_an_approval": True,
        "build_id": build_id,
        "summary_counts": {
            "defect": int(summary.get("DEFECT") or 0),
            "candidate": int(summary.get("CANDIDATE") or 0)},
        "artifact_sha256": artifacts,
        "findings": findings}
    raw = canon_bytes(projection)
    if _scan(raw, emails=True):
        return {"attached": False, "binding": "WITHHELD",
                "not_an_approval": True, "path": None,
                "defect_count": 0, "candidate_count": 0}, None
    binding = "MATCH"
    for name, recorded in artifacts.items():
        path = folder / name
        if not path.is_file() or path.is_symlink() or digest(path) != recorded:
            binding = "STALE"
    qc = {"attached": True, "binding": binding, "not_an_approval": True,
          "path": "qc/diagnostics.json",
          "defect_count": int(summary.get("DEFECT") or 0),
          "candidate_count": int(summary.get("CANDIDATE") or 0)}
    return qc, projection


def _thumbnail(folder, record, destination):
    delivery = (record.get("sequences") or {}).get("delivery_dir")
    source = None
    if type(delivery) is str and delivery:
        try:
            candidate = safe_path(folder, delivery) / frame_filename(0)
        except FilmError:
            candidate = None
        if candidate is not None and candidate.is_file() \
                and not candidate.is_symlink():
            source = candidate
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source is None:
        image = Image.new("RGB", (160, 90), (32, 32, 32))
        image.save(destination, "PNG")
        return "PLACEHOLDER"
    with Image.open(source) as image:
        frame = image.convert("RGB")
        frame.thumbnail((320, 180))
        frame.save(destination, "PNG")
    return "FROM_DELIVERY_FRAME"


def _member(role, path, root, scope_class):
    relative = path.relative_to(root).as_posix()
    if relative.startswith("/") or ".." in relative.split("/"):
        raise FilmError(f"unsafe bundle path: {relative}")
    return {"role": role, "path": relative, "sha256": digest(path),
            "bytes": path.stat().st_size, "scope_class": scope_class}


def _discard(output):
    if output.exists():
        shutil.rmtree(output)


def delivery_status(project, build_id):
    """Read-only Preview / candidate / adoption / publication projection.

    The file names inside the build are not read. LEGACY_MV raises before
    any write.
    """
    project = Path(project)
    require_animation_profile(project)
    build_id = _build_id(build_id)
    folder = safe_path(project, f"builds/{build_id}")
    record_path = folder / "build.json"
    if not record_path.is_file():
        raise FilmError(f"No build {build_id}")
    record = read(record_path)
    preview = _preview(record)
    candidate = _candidate(folder, record, preview)
    scope = _scope_rows(project, folder, build_id)
    profile_name, profile_check, problems = _profile(record, folder)
    clock = _clock(record, folder)
    return {"build_id": build_id,
            "states": _states(preview, candidate, scope),
            "scope_match": scope["scope_match"],
            "profile_check": profile_check,
            "delivery_profile": profile_name,
            "profile_problems": problems,
            "meets_work_contract": _meets(clock),
            "facets": dict(_FACETS)}


def _validate_common_list(items, keys, what):
    if type(items) is not list:
        raise FilmError(f"{what} must be a list")
    for item in items:
        if type(item) is not dict or set(item) != keys:
            raise FilmError(f"Malformed {what} entry")


def validate_scope(document):
    check_document(document, SCOPE_TYPE)
    if set(document) != _SCOPE_KEYS:
        raise FilmError("delivery_review_scope fields do not match the schema")
    if document["locks_are_not_approval"] is not True \
            or document["inherited_reviews"] is not False:
        raise FilmError("review scope must not inherit locks or reviews")
    if document["scope_match"] not in {"MATCH", "MISMATCH", "NO_CURRENT_SCOPE"}:
        raise FilmError("Unknown scope_match")
    _validate_common_list(document["deliverables"], _DELIVERABLE_KEYS,
                          "deliverables")
    _validate_common_list(document["compared"], _COMPARED_KEYS, "compared")
    return document


def validate_bundle(document):
    check_document(document, BUNDLE_TYPE)
    if set(document) != _BUNDLE_KEYS:
        raise FilmError("delivery_bundle fields do not match the schema")
    if set(document["states"]) != _STATE_KEYS:
        raise FilmError("delivery states do not match the schema")
    if document["states"]["filename_is_not_approval"] is not True:
        raise FilmError("filename_is_not_approval must stay true")
    if document["states"]["artwork_acceptance"] != "PENDING":
        raise FilmError("artwork acceptance stays PENDING")
    if document["states"]["external_publication"] != "NOT_AUTHORIZED":
        raise FilmError("external publication stays NOT_AUTHORIZED")
    if document["states"]["preview"] not in {"PREVIEW", "NOT_PREVIEW"}:
        raise FilmError("Unknown preview state")
    if document["states"]["final_candidate"] not in {
            "FINAL_CANDIDATE_READY", "NOT_A_CANDIDATE"}:
        raise FilmError("Unknown final candidate state")
    if document["states"]["director_adoption"] not in {
            "PROTOCOL_ADOPTED", "NOT_ADOPTED"}:
        raise FilmError("Unknown director adoption state")
    if set(document["facets"]) != _FACET_KEYS \
            or document["facets"] != _FACETS:
        raise FilmError("delivery facets are unqualified, pending and "
                        "not authorized")
    if document["work_contract"] != WORK_CONTRACT:
        raise FilmError("work contract must stay 240s at 24/1 fps")
    if document["approval_consistent"] is True \
            and document["states"]["director_adoption"] == "PROTOCOL_ADOPTED" \
            and document["states"]["preview"] == "PREVIEW":
        raise FilmError("a preview cannot be reported as director-adopted")
    _validate_common_list(document["members"], _MEMBER_KEYS, "members")
    _validate_common_list(document["sources"], _SOURCE_KEYS, "sources")
    if set(document["qc"]) != _QC_KEYS or document["qc"]["not_an_approval"] \
            is not True:
        raise FilmError("QC projection is not an approval")
    if tuple(document["notes"]) != _NOTES:
        raise FilmError("delivery notes were changed")
    return document


def verify_bundle(bundle_dir):
    """Re-hash a bundle directory the way a download is checked.

    Extra files, symlinks, a mismatched manifest hash and a secret-shaped
    byte sequence all fail. This does not contact a network and does not
    treat the bundle as released.
    """
    root = Path(bundle_dir)
    manifest = root / "bundle.json"
    sidecar = root / "bundle.sha256"
    errors = []
    if not manifest.is_file() or manifest.is_symlink():
        errors.append("missing bundle.json")
        return {"valid": False, "errors": errors}
    if not sidecar.is_file() or sidecar.is_symlink():
        errors.append("missing bundle.sha256")
    else:
        recorded = sidecar.read_text(encoding="utf-8").strip()
        if recorded != digest(manifest):
            errors.append("bundle.json hash != bundle.sha256")
    try:
        document = validate_bundle(read_canon(manifest))
    except FilmError as exc:
        errors.append(str(exc))
        return {"valid": False, "errors": errors}
    expected = {"bundle.json", "bundle.sha256"}
    for member in document["members"]:
        expected.add(member["path"])
        path = root / member["path"]
        if path.is_symlink() or not path.is_file():
            errors.append(f"missing {member['path']}")
            continue
        if digest(path) != member["sha256"] \
                or path.stat().st_size != member["bytes"]:
            errors.append(f"hash mismatch {member['path']}")
        kind = _scan_file(path)
        if kind:
            errors.append(f"{member['path']} contains a {kind} pattern")
    found = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            errors.append(f"symlink {path.relative_to(root).as_posix()}")
            continue
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
    extra = found - expected
    if extra:
        errors.append("extra files: " + ", ".join(sorted(extra)))
    scope_member = next((m for m in document["members"]
                         if m["role"] == "review_scope"), None)
    if scope_member and not any(e.startswith("missing") or "hash mismatch"
                                in e for e in errors):
        try:
            validate_scope(read_canon(root / scope_member["path"]))
        except FilmError as exc:
            errors.append(str(exc))
    return {"valid": not errors, "errors": errors,
            "bundle_id": document.get("bundle_id"),
            "states": document.get("states"),
            "scope_match": None,
            "facets": dict(_FACETS)}


def assemble_bundle(project, build_id, output_dir):
    """Write a delivery bundle for one sealed build.

    Fails closed: a secret, email, home path or symlink aborts and the
    partial directory is removed. The sealed build and the live lyric,
    audio and approval files are not modified.
    """
    project = Path(project)
    require_animation_profile(project)
    build_id = _build_id(build_id)
    folder = safe_path(project, f"builds/{build_id}")
    record_path = folder / "build.json"
    if not record_path.is_file():
        raise FilmError(f"No build {build_id}")
    output = _output_dir(project, folder, output_dir)
    with project_mutex(project):
        record = read(record_path)
        present = []
        for role, filename in {**_MEDIA, **_SUBS}.items():
            path = folder / filename
            if path.exists():
                present.append((role, filename, path))
        if not any(role in {"clean_master", "subbed_master", "draft_preview"}
                   for role, _, _ in present):
            raise FilmError(
                f"{build_id} has no clean, subbed or preview master to bundle")
        preview = _preview(record)
        candidate = _candidate(folder, record, preview)
        scope = _scope_rows(project, folder, build_id)
        states = _states(preview, candidate, scope)
        profile_name, profile_check, problems = _profile(record, folder)
        sources = _sources(project, folder, record)
        clock = _clock(record, folder)
        qc_meta, qc_body = _qc_projection(project, build_id, folder)
        try:
            output.mkdir(parents=True)
            copied = []
            for role, filename, path in present:
                dest_dir = "subtitles" if role in _SUBS else "media"
                destination = output / dest_dir / filename
                _copy_member(path, destination, filename)
                scope_class = "OUT_OF_SCOPE"
                if role in {"clean_master", "subbed_master"}:
                    row = next((item for item in scope["compared"]
                                if item["role"] == role), None)
                    if row and row["match"]:
                        scope_class = "IN_SCOPE"
                elif role in _SUBS:
                    scope_class = "PROOF"
                copied.append((role, destination, scope_class))
            thumb = output / "media" / "thumbnail.png"
            thumb_kind = _thumbnail(folder, record, thumb)
            kind = _scan_file(thumb)
            if kind:
                raise FilmError(f"thumbnail contains a {kind} pattern")
            copied.append(("thumbnail", thumb, "OUT_OF_SCOPE"))
            if qc_body is not None:
                qc_path = output / "qc" / "diagnostics.json"
                write_canon(qc_path, qc_body)
                copied.append(("qc_report", qc_path, "PROOF"))
            scope_path = output / "review" / "scope.json"
            write_canon(scope_path, scope)
            validate_scope(read_canon(scope_path))
            copied.append(("review_scope", scope_path, "PROOF"))
            in_scope = {row["role"]: row for row in scope["compared"]
                        if row["match"]}
            members = [_member(role, path, output, scope_class)
                       for role, path, scope_class in copied]
            # Included in-scope hashes are the approval scope hashes.
            for member in members:
                if member["scope_class"] != "IN_SCOPE":
                    continue
                bound = in_scope.get(member["role"])
                if bound is None or member["sha256"] != bound["artifact_sha256"] \
                        or member["sha256"] != bound["scope_sha256"]:
                    raise FilmError(
                        "included artifact hash does not match the current "
                        "approval scope")
            both = {"clean_master", "subbed_master"} <= {m["role"]
                                                         for m in members}
            subs = {"subtitle_ass", "subtitle_srt"} <= {m["role"]
                                                        for m in members}
            document = {
                "document_type": BUNDLE_TYPE,
                "schema_version": SCHEMA,
                "bundle_id": f"DLV-{build_id}",
                "project_id": project.name,
                "build_id": build_id,
                "created_at": now(),
                "delivery_profile": profile_name,
                "profile_check": profile_check,
                "profile_problems": problems,
                "storage_profile": record.get("storage_profile")
                if type(record.get("storage_profile")) is str
                else "UNKNOWN",
                "delivery_complete": bool(both and subs and thumb.is_file()),
                "approval_consistent": scope["scope_match"] != "MISMATCH",
                "members": members,
                "sources": sources,
                "qc": qc_meta,
                "states": states,
                "facets": dict(_FACETS),
                "work_contract": dict(WORK_CONTRACT),
                "delivery_clock": clock,
                "meets_work_contract": _meets(clock),
                "notes": list(_NOTES)}
            write_canon(output / "bundle.json", document)
            validate_bundle(read_canon(output / "bundle.json"))
            sidecar = output / "bundle.sha256"
            sidecar.write_text(digest(output / "bundle.json") + "\n",
                               encoding="utf-8")
            checked = verify_bundle(output)
            if not checked["valid"]:
                raise FilmError(
                    "bundle failed its own download check: "
                    + "; ".join(checked["errors"]))
        except Exception:
            _discard(output)
            raise
    checked_scope = validate_scope(read_canon(output / "review" / "scope.json"))
    return {"bundle_dir": str(output),
            "bundle_id": document["bundle_id"],
            "bundle_sha256": (output / "bundle.sha256").read_text(
                encoding="utf-8").strip(),
            "states": states,
            "scope_match": checked_scope["scope_match"],
            "profile_check": profile_check,
            "delivery_complete": document["delivery_complete"],
            "approval_consistent": document["approval_consistent"],
            "meets_work_contract": document["meets_work_contract"],
            "thumbnail": thumb_kind,
            "facets": dict(_FACETS),
            "valid": True}
