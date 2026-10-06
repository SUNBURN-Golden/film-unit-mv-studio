"""Archive commit / seal state machine with durable reconciliation
(ANIM-021, schema §16).

commit key = sha256 over the canon tuple of build id + snapshot + the
ordered required-artifact pin list + recipe + toolchain + verification
profile. Stages:

    OBJECTS_PENDING -> OBJECTS_VERIFIED -> MANIFEST_INTENT -> SEALED
                                              |
                                              +-> SEAL_UNKNOWN (publication
                                                  outcome undetermined)

- COMMIT_INTENT is journaled before the first upload side effect and
  pins every required artifact by object id + sha256 + byte length +
  its minimum verification level.
- Each object stages through the journaled UploadTracker (immutable,
  content-addressed staging) and is then classified by verify_object.
  `UPLOADED_UNVERIFIED` never satisfies a seal: every required object
  must reach at least its declared minimum — `UPLOAD_HASH_MATCHED` when a
  trustworthy provider SHA-256 exists, bounded `FULL_READBACK` otherwise.
- MANIFEST_INTENT records the exact manifest bytes (the `archive_seal`
  document) and the journal head reference durably, BEFORE the manifest
  is published.
- Publication is the same bytes under the same object id — never a new
  manifest. A lost response leaves the commit SEAL_UNKNOWN; an explicit
  `reconcile` answers the publication question by reading the objects
  back and comparing bytes, not by matching names. Multiple manifest
  objects for one build are detected and refused; nothing is deleted and
  a past approved seal is never overwritten.
- Nothing is deleted except objects the caller lists in an explicit
  `sweep_orphans` approval — never a manifest, never an object any commit
  references.

This module makes no atomic-commit or WORM claim about the backend: the
seal is a manifest the coordinator can later re-read and re-check.
"""
import hashlib
from pathlib import Path
import threading

from .animation_schema import canon_bytes, check_document
from .archive_manifest import (DEFAULT_READBACK_CAP, LEVELS, bounded_read,
                               verify_object)
from .core import FilmError, now
from .durable_journal import DurableJournal
from .fav_pack import (build_pack, image_contract_for, index_bytes,
                       make_index, sha256_bytes)
from .storage_backends import (ArchiveRequestError, ConnectionDropped,
                               check_object_id)
from .upload_tracker import UploadTracker

SEAL_TYPE = "archive_seal"
SEAL_FIELDS = {"document_type", "schema_version", "commit_key", "build_id",
               "snapshot_digest", "recipe_digest", "toolchain_digest",
               "artifacts", "coverage", "render_contract",
               "encode_contract", "verification_contract", "journal_ref",
               "created_at", "qualification"}
STAGES = {"OBJECTS_PENDING", "OBJECTS_VERIFIED", "MANIFEST_INTENT",
          "SEALED", "SEAL_UNKNOWN"}
MANIFEST_PREFIX = "manifest-"
DEFAULT_PROFILE = {"min_level": "UPLOAD_HASH_MATCHED", "per_kind": {},
                   "readback_cap_bytes": DEFAULT_READBACK_CAP}
# Execution-storage §9.1: the completion manifest stays unpublished
# until the source, the delivery PNG sequence, the clean reproduction
# recipe, the frame_map, the original audio, the cue/font artifacts and
# both clean and subbed MP4s are stored and verified. This is the
# default `required_kinds` for begin_commit; an explicit reduced set is
# accepted only for FAKE unit-test fixtures — never for a real
# completion seal.
COMPLETION_REQUIRED_KINDS = (
    "source", "delivery_png_sequence", "recipe_clean", "frame_map",
    "audio_original", "cue_font", "mp4_clean", "mp4_subbed")


def _sha(value, what):
    if type(value) is not str or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise FilmError(f"{what} must be a lowercase SHA-256")
    return value


def _level_rank(level):
    if level not in LEVELS:
        raise FilmError(f"Unknown verification level: {level}")
    return LEVELS[level]


def manifest_object_id(build_id, manifest_sha256):
    return f"{MANIFEST_PREFIX}{build_id}-{manifest_sha256[:16]}"


def validate_archive_seal(document):
    """The `archive_seal` 1 manifest contract (schema §16)."""
    check_document(document, SEAL_TYPE)
    if type(document) is not dict or set(document.keys()) != SEAL_FIELDS:
        raise FilmError(f"archive_seal must hold {sorted(SEAL_FIELDS)}")
    for field in ("commit_key", "snapshot_digest", "recipe_digest",
                  "toolchain_digest", "verification_contract"):
        _sha(document.get(field), field)
    if type(document.get("build_id")) is not str or not document["build_id"]:
        raise FilmError("archive_seal build_id must be a non-empty string")
    artifacts = document.get("artifacts")
    if type(artifacts) is not list or not artifacts:
        raise FilmError("archive_seal artifacts must be a non-empty list")
    for entry in artifacts:
        if type(entry) is not dict or set(entry.keys()) != \
                {"kind", "objects"}:
            raise FilmError("archive_seal artifacts hold kind + objects")
        for obj in entry["objects"]:
            if type(obj) is not dict or set(obj.keys()) != \
                    {"object_id", "sha256", "byte_length", "level",
                     "min_level"}:
                raise FilmError("archive_seal object entries are fixed")
            _sha(obj["sha256"], "archive_seal object sha256")
            if obj["level"] not in LEVELS or obj["min_level"] not in LEVELS:
                raise FilmError("archive_seal object levels are fixed")
            if _level_rank(obj["level"]) < _level_rank(obj["min_level"]):
                raise FilmError("archive_seal holds an object below its "
                                "minimum verification level")
    journal_ref = document.get("journal_ref")
    if type(journal_ref) is not dict or set(journal_ref.keys()) != \
            {"head_digest", "records"}:
        raise FilmError("archive_seal journal_ref pins head_digest+records")
    _sha(journal_ref["head_digest"], "journal_ref.head_digest")
    if type(journal_ref["records"]) is not int or journal_ref["records"] < 0:
        raise FilmError("journal_ref.records must be an integer >= 0")
    if type(document.get("qualification")) is not dict:
        raise FilmError("archive_seal carries the qualification facets")
    return document


class ArchiveCommitter:
    """Stages, verifies and seals one build's archive on a backend.

    The commit journal in `commit_dir` is the durable record; a restart
    replays it and continues the same commit identity — re-uploading the
    same content-addressed bytes is a no-op, verified objects are reused
    only from their OBJECT_VERIFIED records, and manifest publication is
    reconciled, never duplicated.
    """

    def __init__(self, commit_dir, backend, *, upload=None, retry=None,
                 session_store=None, sleep_fn=None):
        self.commit_dir = Path(commit_dir)
        self.commit_dir.mkdir(parents=True, exist_ok=True)
        self.backend = backend
        self.retry = retry
        self.sleep = sleep_fn or (lambda ms: None)
        self.journal = DurableJournal(self.commit_dir / "job_journal.jsonl")
        self.tracker = UploadTracker(self.journal, backend, upload=upload,
                                     retry=retry,
                                     session_store=session_store,
                                     sleep_fn=self.sleep)
        self.commits = {}
        self.orphans_deleted = []
        try:
            self._replay(self.journal.records)
        except FilmError as e:
            # Inconsistent history: keep the valid prefix, fence writes —
            # never repaired into a success state.
            self.journal.fence(f"replay inconsistency: {e}")

    @property
    def fenced(self):
        return self.journal.fenced

    def _require_writable(self):
        if self.journal.fenced:
            status = self.journal.status()
            raise FilmError(
                "JOURNAL_RECONCILIATION_REQUIRED: the commit journal is "
                f"fenced ({status['tail_reason'] or status['fence_reason']})")

    def _j(self, event, data):
        self.journal.append("commit", event, data)

    # -- replay ------------------------------------------------------------------
    def _replay(self, records):
        for record in records:
            event, data = record["event"], record["data"]
            if record["scope"] != "commit":
                continue            # upload tracker records are its own
            key = data.get("commit_key")
            if event == "COMMIT_INTENT":
                if key in self.commits:
                    raise FilmError(f"journal re-commits {key}")
                self.commits[key] = {
                    "commit_key": key, "build_id": data["build_id"],
                    "snapshot_digest": data["snapshot_digest"],
                    "recipe_digest": data["recipe_digest"],
                    "toolchain_digest": data["toolchain_digest"],
                    "verification_profile": data["verification_profile"],
                    "artifacts": data["artifacts"],
                    "coverage": data.get("coverage"),
                    "render_contract": data.get("render_contract"),
                    "encode_contract": data.get("encode_contract"),
                    "verified": {}, "state": "OBJECTS_PENDING",
                    "manifest": None, "manifest_object_id": None,
                    "manifest_sha256": None, "seal": None}
            elif event == "ORPHAN_DELETED":
                self.orphans_deleted.append(data["object_id"])
            elif key not in self.commits:
                raise FilmError(f"journal {event} names unknown commit")
            elif event == "OBJECT_VERIFIED":
                self.commits[key]["verified"][data["object_id"]] = \
                    data["level"]
            elif event == "OBJECTS_VERIFIED":
                self.commits[key]["state"] = "OBJECTS_VERIFIED"
            elif event == "MANIFEST_INTENT":
                commit = self.commits[key]
                commit["manifest"] = data["manifest"]
                commit["manifest_object_id"] = data["manifest_object_id"]
                commit["manifest_sha256"] = data["manifest_sha256"]
                commit["state"] = "MANIFEST_INTENT"
            elif event == "MANIFEST_PUBLISHED":
                self.commits[key]["state"] = "MANIFEST_INTENT"
            elif event == "SEALED":
                self.commits[key]["state"] = "SEALED"
                self.commits[key]["seal"] = data
            elif event == "SEAL_UNKNOWN":
                self.commits[key]["state"] = "SEAL_UNKNOWN"
            # UPLOAD_* records are the tracker's stream; replayed there.

    # -- commit lifecycle ----------------------------------------------------------
    def _normalize_artifacts(self, artifacts, profile):
        """Pin every declared artifact's objects by content hash.

        Each artifact is {"kind": ..., "data": bytes} for a raw object or
        {"kind": ..., "members": [...], "image_contract": ...,
         "frame_coverage": ..., "snapshot_digest"/"recipe_digest"/
         "sequence_digest" pins} for a FAV1 pack + index pair. Returned
        objects carry bytes only in memory — the journal stores pins.
        """
        normalized = []
        seen_objects = set()
        for position, artifact in enumerate(artifacts):
            if type(artifact) is not dict \
                    or type(artifact.get("kind")) is not str \
                    or not artifact["kind"]:
                raise FilmError(f"commit artifact {position} needs a kind")
            min_level = artifact.get("min_level") or \
                profile["per_kind"].get(artifact["kind"],
                                        profile["min_level"])
            objects = []
            if "members" in artifact:
                pack_bytes, entries = build_pack(artifact["members"])
                contract = artifact.get("image_contract") or \
                    image_contract_for([m["data"] for m in
                                        artifact["members"]])
                coverage = artifact.get("frame_coverage")
                if coverage is None:
                    indices = sorted(m["frame_index"] for m in entries
                                     if m["frame_index"] is not None)
                    if not indices:
                        raise FilmError("frame_coverage is required when "
                                        "no member carries a frame_index")
                    coverage = [indices[0], indices[-1] + 1]
                index_doc = make_index(
                    pack_bytes, entries, image_contract=contract,
                    frame_coverage=coverage,
                    snapshot_digest=artifact["snapshot_digest"],
                    recipe_digest=artifact["recipe_digest"],
                    sequence_digest=artifact["sequence_digest"],
                    retention_refs=list(artifact.get("retention_refs", ())))
                parts = [("pack", pack_bytes), ("pack_index",
                                               index_bytes(index_doc))]
            else:
                parts = [(artifact["kind"], artifact["data"])]
            for kind, data in parts:
                object_id = f"sha256-{sha256_bytes(data)}"
                if object_id in seen_objects:
                    raise FilmError(f"Duplicate commit object {object_id}")
                seen_objects.add(object_id)
                objects.append({"object_id": object_id,
                                "kind": kind,
                                "sha256": sha256_bytes(data),
                                "byte_length": len(data),
                                "min_level": min_level,
                                "data": data})
            normalized.append({"kind": artifact["kind"],
                               "objects": objects})
        return normalized

    @staticmethod
    def _pins(artifacts):
        return [{"kind": a["kind"],
                 "objects": [{"object_id": o["object_id"],
                              "sha256": o["sha256"],
                              "byte_length": o["byte_length"],
                              "min_level": o["min_level"]}
                             for o in a["objects"]]}
                for a in artifacts]

    def begin_commit(self, build_id, artifacts, *, snapshot_digest,
                     recipe_digest, toolchain_digest,
                     verification_profile=None, required_kinds=None,
                     coverage=None, render_contract=None,
                     encode_contract=None):
        """Journal COMMIT_INTENT before any upload side effect.

        `required_kinds=None` demands the full §9.1 completion inventory
        (COMPLETION_REQUIRED_KINDS); passing a smaller set is a FAKE
        unit-test fixture escape hatch only — the completion seal path
        never narrows it.
        """
        if type(build_id) is not str or not build_id:
            raise FilmError("commit needs a non-empty build_id")
        # The manifest object id embeds the build id — fail the intent
        # before any side effect when it cannot form a valid object id.
        check_object_id(manifest_object_id(build_id, "0" * 64))
        profile = dict(DEFAULT_PROFILE,
                       **(verification_profile or {}))
        _level_rank(profile["min_level"])
        normalized = self._normalize_artifacts(artifacts, profile)
        if required_kinds is None:
            required_kinds = COMPLETION_REQUIRED_KINDS
        missing = [k for k in required_kinds
                   if k not in {a["kind"] for a in normalized}]
        if missing:
            raise FilmError(f"commit is missing required artifacts: "
                            f"{missing}")
        pins = self._pins(normalized)
        commit_key = hashlib.sha256(canon_bytes({
            "build_id": build_id, "snapshot_digest": snapshot_digest,
            "artifacts": pins, "recipe_digest": recipe_digest,
            "toolchain_digest": toolchain_digest,
            "verification_profile": profile})).hexdigest()
        if commit_key in self.commits:
            return commit_key                      # same intent, idempotent
        existing = [c for c in self.commits.values()
                    if c["build_id"] == build_id]
        if existing:
            raise FilmError(
                "BUILD_IDENTITY_CONFLICT: build "
                f"{build_id} already holds commit "
                f"{existing[0]['commit_key'][:16]}; a different manifest "
                "for the same build is refused, never overwritten")
        self._j("COMMIT_INTENT", {
            "commit_key": commit_key, "build_id": build_id,
            "snapshot_digest": snapshot_digest,
            "recipe_digest": recipe_digest,
            "toolchain_digest": toolchain_digest,
            "verification_profile": profile,
            "artifacts": pins,
            "coverage": coverage,
            "render_contract": render_contract,
            "encode_contract": encode_contract})
        commit = {"commit_key": commit_key, "build_id": build_id,
                  "snapshot_digest": snapshot_digest,
                  "recipe_digest": recipe_digest,
                  "toolchain_digest": toolchain_digest,
                  "verification_profile": profile,
                  "artifacts": pins, "verified": {},
                  "state": "OBJECTS_PENDING", "manifest": None,
                  "manifest_object_id": None, "manifest_sha256": None,
                  "seal": None, "coverage": coverage,
                  "render_contract": render_contract,
                  "encode_contract": encode_contract,
                  # Staged bytes live in memory only — the journal pins
                  # them by hash; a restart re-supplies them via run().
                  "_data": {o["object_id"]: o["data"]
                            for a in normalized for o in a["objects"]}}
        self.commits[commit_key] = commit
        return commit_key

    def _objects_for(self, commit, artifacts):
        """Re-derive staged objects from caller-supplied bytes and check
        they are exactly the pinned intent — a commit never drifts."""
        if artifacts is None:
            staged = commit.get("_data")
            if not staged:
                raise FilmError("COMMIT_BYTES_REQUIRED: object bytes are "
                                "not journaled; re-supply the same "
                                "artifacts after a restart")
            return {object_id: {"object_id": object_id, "data": data}
                    for object_id, data in staged.items()}
        normalized = self._normalize_artifacts(
            artifacts, commit["verification_profile"])
        if self._pins(normalized) != commit["artifacts"]:
            raise FilmError("COMMIT_IDENTITY_MISMATCH: supplied artifacts "
                            "do not reproduce the journaled intent")
        out = {}
        for artifact in normalized:
            for obj in artifact["objects"]:
                out[obj["object_id"]] = obj
        commit["_data"] = {object_id: o["data"]
                           for object_id, o in out.items()}
        return out

    def run(self, commit_key, artifacts=None):
        """Drive one commit to SEALED or SEAL_UNKNOWN.

        `artifacts` (same declaration as begin_commit) re-supplies the
        bytes after a restart; they are checked against the intent pins.
        A journaled OBJECT_VERIFIED is reused only after the object is
        re-checked — existence, length and hash evidence must still hold
        on every run; a deleted or truncated object refuses, it is never
        copied into a sealed manifest.
        """
        self._require_writable()
        commit = self.commits.get(commit_key)
        if commit is None:
            raise FilmError(f"Unknown commit: {commit_key}")
        if commit["state"] == "SEALED":
            return {"commit_key": commit_key, "state": "SEALED",
                    "manifest_object_id": commit["manifest_object_id"]}
        if commit["state"] in {"MANIFEST_INTENT", "SEAL_UNKNOWN"}:
            return self.reconcile(commit_key)
        objects = self._objects_for(commit, artifacts)
        if commit["state"] == "OBJECTS_PENDING":
            profile = commit["verification_profile"]
            cap = profile.get("readback_cap_bytes")
            for artifact in commit["artifacts"]:
                for pin in artifact["objects"]:
                    if pin["object_id"] not in commit["verified"]:
                        obj = objects[pin["object_id"]]
                        self.tracker.put(pin["object_id"], obj["data"])
                    level = self._verify_pinned(pin, cap)
                    if _level_rank(level) < _level_rank(pin["min_level"]):
                        raise FilmError(
                            f"OBJECT_LEVEL_INSUFFICIENT: "
                            f"{pin['object_id']} reached {level} below "
                            f"{pin['min_level']}")
                    if pin["object_id"] in commit["verified"]:
                        # The journaled checkpoint still stands — the
                        # live re-check above is what makes it reusable.
                        continue
                    self._j("OBJECT_VERIFIED", {
                        "commit_key": commit_key,
                        "object_id": pin["object_id"], "level": level,
                        "min_level": pin["min_level"]})
                    commit["verified"][pin["object_id"]] = level
            self._j("OBJECTS_VERIFIED", {"commit_key": commit_key})
            commit["state"] = "OBJECTS_VERIFIED"
        if commit["state"] == "OBJECTS_VERIFIED":
            return self._publish(commit)
        return {"commit_key": commit_key, "state": commit["state"]}

    def _verify_pinned(self, pin, cap):
        """Live verification for one pinned object — run every time.

        A provider-reported SHA-256 never clears a FULL_READBACK floor:
        when the pin's declared minimum is readback-class the whole
        object is re-read and hashed; OBJECT_LEVEL_INSUFFICIENT can be
        refused only after that readback was actually attempted.
        """
        if _level_rank(pin["min_level"]) >= LEVELS["FULL_READBACK"]:
            return self._full_readback(pin, cap)
        return verify_object(
            self.backend, pin["object_id"], pin["sha256"],
            pin["byte_length"], readback_cap=cap,
            retry=self.retry, sleep_fn=self.sleep)

    def _full_readback(self, pin, cap):
        """Bounded whole-object re-read — the only FULL_READBACK proof."""
        if pin["byte_length"] > (cap or 0):
            raise FilmError(
                f"READBACK_CAP_EXCEEDED: object {pin['object_id']} needs "
                f"{pin['byte_length']} readback bytes > cap {cap}")
        info = bounded_read(
            lambda: self.backend.object_info(pin["object_id"]),
            self.retry, self.sleep)
        if info.get("byte_length") != pin["byte_length"]:
            raise FilmError(
                f"Archive object {pin['object_id']} stored length "
                f"{info.get('byte_length')} != manifest "
                f"{pin['byte_length']}")
        result = bounded_read(
            lambda: self.backend.get_object(pin["object_id"]),
            self.retry, self.sleep)
        if result.status != 200 or len(result.body) != pin["byte_length"]:
            raise FilmError(f"Archive object {pin['object_id']} readback "
                            "incomplete")
        if sha256_bytes(result.body) != pin["sha256"]:
            raise FilmError(f"Archive object {pin['object_id']} readback "
                            "hash mismatch")
        return "FULL_READBACK"

    def _manifest_doc(self, commit):
        profile = commit["verification_profile"]
        return {"document_type": SEAL_TYPE, "schema_version": 1,
                "commit_key": commit["commit_key"],
                "build_id": commit["build_id"],
                "snapshot_digest": commit["snapshot_digest"],
                "recipe_digest": commit["recipe_digest"],
                "toolchain_digest": commit["toolchain_digest"],
                "artifacts": [{"kind": a["kind"],
                               "objects": [
                                   {"object_id": o["object_id"],
                                    "sha256": o["sha256"],
                                    "byte_length": o["byte_length"],
                                    "level":
                                    commit["verified"][o["object_id"]],
                                    "min_level": o["min_level"]}
                                   for o in a["objects"]]}
                              for a in commit["artifacts"]],
                "coverage": commit.get("coverage"),
                "render_contract": commit.get("render_contract"),
                "encode_contract": commit.get("encode_contract"),
                "verification_contract": hashlib.sha256(
                    canon_bytes(profile)).hexdigest(),
                "journal_ref": {"head_digest": self.journal.head,
                                "records": len(self.journal.records)},
                "created_at": now(),
                # Fake/local backend evidence never qualifies a real
                # archive — the seal reports UNQUALIFIED honestly.
                "qualification": {"node_state": "IN_PROGRESS",
                                  "qualification_state": "UNQUALIFIED",
                                  "acceptance_state": "PENDING",
                                  "release_state": "NOT_AUTHORIZED"}}

    def _publish(self, commit):
        """MANIFEST_INTENT before the publish side effect; SEAL_UNKNOWN
        when the outcome cannot be confirmed — never a second manifest."""
        if commit["manifest"] is None:
            manifest = validate_archive_seal(self._manifest_doc(commit))
            manifest_raw = canon_bytes(manifest)
            manifest_sha = sha256_bytes(manifest_raw)
            object_id = manifest_object_id(commit["build_id"], manifest_sha)
            self._j("MANIFEST_INTENT", {
                "commit_key": commit["commit_key"],
                "manifest_object_id": object_id,
                "manifest_sha256": manifest_sha,
                "manifest": manifest})
            commit.update(manifest=manifest, manifest_object_id=object_id,
                          manifest_sha256=manifest_sha,
                          state="MANIFEST_INTENT")
        manifest_raw = canon_bytes(commit["manifest"])
        try:
            candidates = self._manifest_candidates(commit)
            foreign = [oid for oid in candidates
                       if oid != commit["manifest_object_id"]]
            if foreign or len(candidates) > 1:
                # Another manifest object already names this build:
                # refuse to add a second — preserved, reconciled never
                # silently.
                self._j("SEAL_UNKNOWN", {
                    "commit_key": commit["commit_key"],
                    "reason": f"foreign manifest {foreign or candidates} "
                              "for this build; refused"})
                commit["state"] = "SEAL_UNKNOWN"
                return {"commit_key": commit["commit_key"],
                        "state": "SEAL_UNKNOWN",
                        "candidates": candidates}
            # Publication is never retried on the HTTP allowance: a lost
            # answer leaves SEAL_UNKNOWN and reconcile() answers it.
            self.backend.put_object(commit["manifest_object_id"],
                                    manifest_raw)
        except (ConnectionDropped, ArchiveRequestError) as e:
            # Publication outcome unknown: fence at SEAL_UNKNOWN. Nothing
            # new is created — reconcile() answers the question.
            self._j("SEAL_UNKNOWN", {"commit_key": commit["commit_key"],
                                     "reason": f"publication answer lost: "
                                               f"{e}"})
            commit["state"] = "SEAL_UNKNOWN"
            return {"commit_key": commit["commit_key"],
                    "state": "SEAL_UNKNOWN",
                    "manifest_object_id": commit["manifest_object_id"]}
        self._j("MANIFEST_PUBLISHED", {
            "commit_key": commit["commit_key"],
            "manifest_object_id": commit["manifest_object_id"],
            "manifest_sha256": commit["manifest_sha256"]})
        self._j("SEALED", {
            "commit_key": commit["commit_key"],
            "build_id": commit["build_id"],
            "manifest_object_id": commit["manifest_object_id"],
            "manifest_sha256": commit["manifest_sha256"],
            "artifacts": commit["manifest"]["artifacts"]})
        commit["state"] = "SEALED"
        commit["seal"] = {"manifest_object_id": commit["manifest_object_id"],
                          "manifest_sha256": commit["manifest_sha256"]}
        return {"commit_key": commit["commit_key"], "state": "SEALED",
                "manifest_object_id": commit["manifest_object_id"],
                "manifest_sha256": commit["manifest_sha256"]}

    # -- reconciliation --------------------------------------------------------------
    def _manifest_candidates(self, commit):
        prefix = f"{MANIFEST_PREFIX}{commit['build_id']}-"
        return [oid for oid in
                bounded_read(lambda: self.backend.list_objects(),
                             self.retry, self.sleep)
                if oid.startswith(prefix)]

    def reconcile(self, commit_key):
        """Explicitly resolve MANIFEST_INTENT / SEAL_UNKNOWN.

        The same manifest bytes under the same build identity are checked
        by re-reading and hashing the objects — a name match alone never
        counts. No answer stays SEAL_UNKNOWN; multiple candidate manifests
        for one build are refused.
        """
        commit = self.commits.get(commit_key)
        if commit is None:
            raise FilmError(f"Unknown commit: {commit_key}")
        if commit["state"] == "SEALED":
            return self._seal_status(commit)
        if commit["state"] not in {"MANIFEST_INTENT", "SEAL_UNKNOWN"}:
            return {"commit_key": commit_key, "state": commit["state"]}
        self._require_writable()
        manifest_sha = commit["manifest_sha256"]
        manifest_raw = canon_bytes(commit["manifest"])
        target = commit["manifest_object_id"]
        try:
            candidates = self._manifest_candidates(commit)
            published = False
            if candidates == [target]:
                # Only the intent's own object counts: it is re-read and
                # hashed — a name match alone never seals.
                body = bounded_read(
                    lambda: self.backend.get_object(target),
                    self.retry, self.sleep).body
                published = sha256_bytes(body) == manifest_sha
                if not published:
                    self._j("SEAL_UNKNOWN", {
                        "commit_key": commit_key,
                        "reason": f"the intent manifest {target} holds "
                                  "foreign bytes; refused, preserved"})
                    commit["state"] = "SEAL_UNKNOWN"
                    return {"commit_key": commit_key,
                            "state": "SEAL_UNKNOWN",
                            "candidates": candidates,
                            "reason": "intent manifest integrity failed"}
            elif candidates:
                # A foreign manifest — even one holding byte-identical
                # content — or several candidates name this build: the
                # intent id is never sealed on another object's bytes.
                self._j("SEAL_UNKNOWN", {
                    "commit_key": commit_key,
                    "reason": f"manifest candidates {candidates} do not "
                              f"resolve to the intent {target}"})
                commit["state"] = "SEAL_UNKNOWN"
                return {"commit_key": commit_key, "state": "SEAL_UNKNOWN",
                        "candidates": candidates,
                        "reason": "multiple/foreign manifests for one build"}
        except (ConnectionDropped, ArchiveRequestError) as e:
            self._j("SEAL_UNKNOWN", {"commit_key": commit_key,
                                     "reason": f"publication check "
                                               f"undetermined: {e}"})
            commit["state"] = "SEAL_UNKNOWN"
            return {"commit_key": commit_key, "state": "SEAL_UNKNOWN",
                    "reason": "publication undetermined"}
        if not published:
            # Definitively unpublished: re-PUT the exact same bytes under
            # the same object id — identical publication, not a
            # duplicate, and never retried on the HTTP allowance.
            try:
                self.backend.put_object(target, manifest_raw)
            except (ConnectionDropped, ArchiveRequestError) as e:
                self._j("SEAL_UNKNOWN", {
                    "commit_key": commit_key,
                    "reason": f"re-publish answer lost: {e}"})
                commit["state"] = "SEAL_UNKNOWN"
                return {"commit_key": commit_key, "state": "SEAL_UNKNOWN",
                        "reason": "publication undetermined"}
        self._j("MANIFEST_PUBLISHED", {
            "commit_key": commit_key,
            "manifest_object_id": commit["manifest_object_id"],
            "manifest_sha256": manifest_sha})
        self._j("SEALED", {"commit_key": commit_key,
                           "build_id": commit["build_id"],
                           "manifest_object_id": commit["manifest_object_id"],
                           "manifest_sha256": manifest_sha,
                           "artifacts": commit["manifest"]["artifacts"]})
        commit["state"] = "SEALED"
        commit["seal"] = {"manifest_object_id": commit["manifest_object_id"],
                          "manifest_sha256": manifest_sha}
        return {"commit_key": commit_key, "state": "SEALED",
                "manifest_object_id": commit["manifest_object_id"]}

    def _seal_status(self, commit):
        """Past seals are preserved; a vanished manifest is reported
        ARCHIVE_UNAVAILABLE / INTEGRITY_FAILED, never rewritten."""
        try:
            body = bounded_read(lambda: self.backend.get_object(
                commit["manifest_object_id"]), self.retry, self.sleep).body
        except ArchiveRequestError as e:
            return {"commit_key": commit["commit_key"], "state": "SEALED",
                    "manifest_state": ("ARCHIVE_UNAVAILABLE"
                                       if e.status == 404 else str(e))}
        except ConnectionDropped:
            return {"commit_key": commit["commit_key"], "state": "SEALED",
                    "manifest_state": "ARCHIVE_UNAVAILABLE"}
        manifest_state = ("SEALED" if sha256_bytes(body)
                          == commit["manifest_sha256"]
                          else "INTEGRITY_FAILED")
        return {"commit_key": commit["commit_key"], "state": "SEALED",
                "manifest_state": manifest_state,
                "manifest_object_id": commit["manifest_object_id"]}

    # -- approved orphan sweep --------------------------------------------------------
    def sweep_orphans(self, approved):
        """Delete only the explicitly approved object ids.

        A manifest object or an object any commit references is refused;
        anything not listed is never touched. Each removal is journaled.
        """
        self._require_writable()
        protected = set()
        for commit in self.commits.values():
            for artifact in commit["artifacts"]:
                for obj in artifact["objects"]:
                    protected.add(obj["object_id"])
            if commit.get("manifest_object_id"):
                protected.add(commit["manifest_object_id"])
        deleted = []
        for object_id in approved:
            if object_id.startswith(MANIFEST_PREFIX):
                raise FilmError(f"ORPHAN_REFUSED: {object_id} is a manifest "
                                "object; manifests are never orphans")
            if object_id in protected:
                raise FilmError(f"ORPHAN_REFUSED: {object_id} is referenced "
                                "by a commit")
            # The approved deletion is durable BEFORE the irreversible
            # side effect — a crash can never leave an unrecorded
            # removal; reconcile re-checks existence against the record.
            self._j("ORPHAN_DELETED", {"object_id": object_id})
            bounded_read(lambda oid=object_id:
                         self.backend.delete_object(oid),
                         self.retry, self.sleep)
            deleted.append(object_id)
            self.orphans_deleted.append(object_id)
        return {"deleted": deleted}

    # -- status ----------------------------------------------------------------------
    def status(self):
        return {"journal": self.journal.status(),
                "fenced": self.journal.fenced,
                "commits": [
                    {"commit_key": c["commit_key"],
                     "build_id": c["build_id"],
                     "state": c["state"],
                     "verified": sorted(c["verified"]),
                     "manifest_object_id": c["manifest_object_id"],
                     "manifest_sha256": c["manifest_sha256"]}
                    for c in self.commits.values()],
                "orphans_deleted": list(self.orphans_deleted),
                "uploads": self.tracker.status()}


def seal_reconcile(commit_dir, backend, *, upload=None, retry=None,
                   sleep_fn=None):
    """Reconcile every commit the journal in `commit_dir` knows."""
    committer = ArchiveCommitter(commit_dir, backend, upload=upload,
                                 retry=retry, sleep_fn=sleep_fn)
    report = {"journal": committer.journal.status(),
              "fenced": committer.fenced,
              "commits": []}
    for commit in committer.commits.values():
        if committer.fenced:
            report["commits"].append({"commit_key": commit["commit_key"],
                                      "state": commit["state"],
                                      "fenced": True})
        elif commit["state"] in {"MANIFEST_INTENT", "SEAL_UNKNOWN"}:
            report["commits"].append(committer.reconcile(
                commit["commit_key"]))
        elif commit["state"] == "SEALED":
            report["commits"].append(
                committer._seal_status(commit))
        else:
            report["commits"].append({"commit_key": commit["commit_key"],
                                      "state": commit["state"]})
    return report
