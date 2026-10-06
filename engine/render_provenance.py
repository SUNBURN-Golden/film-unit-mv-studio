"""ANIM-020 — canonical render provenance · invalidate closure · partial rebuild.

Design §11.1–11.5 and execution/storage §8.2.1/§10–11, layered on top of the
ANIM-017 scheduler (`engine/perf_scheduler.py`) — the dependency graph, the
content keys and the invalidation machinery are reused, never forked. This
module adds:

- `content_digest`: sha256 over ANIM-001 canonical bytes of content-only
  material. Integers, rationals (`[num, den]` reduced pairs), ordered
  source/frame/weight data and the qualified toolchain hash are fixed;
  locators, runtime/job status, approval state and a document's own digest
  are *rejected* as material, not merely ignored.
- the canonical `render_manifest` (schema_version 1): the ordered layered
  digests — snapshot → source → recipe/toolchain → clean/subbed sequence
  roots → encode digests — plus separately recorded artifact hashes.
- `plan_rebuild`: the change-closure rebuild plan. A declared change class
  (picture/sequence/used-range/transition/font/cue/encoder/locator/
  unadopted-candidate/memo/preparation/color-tool) maps to the actual
  read/decode/compose/encode scope, the source/member/halo read plan and
  the stale-approval projection — computed at review-binding granularity,
  so the projection matches what `review_status`/`lock_status` will report
  after the change is applied.
- `provenance_report` / `rebuild_plan_report`: the CLI payloads.
- `compare_runs` / `actual_scope`: cold-vs-warm equality for the impact
  hashes and the frame_map, plus the executed read/decode/compose/encode
  ranges of a run.

Scope honesty: a "replay" here means re-running the deterministic compose
from recorded content keys — never a generation re-call; GPU byte-level
reproducibility is outside this slice and is never claimed. Hardware,
remote and external-service facets remain UNQUALIFIED / PENDING /
NOT_AUTHORIZED.
"""
from pathlib import Path
import copy
import hashlib
import platform
import re

from .animation_locks import load_waves, lock_status
from .animation_review import review_status
from .animation_schema import (canon_bytes, check_document,
                               load_animation_timeline,
                               require_animation_profile)
from .core import FilmError, project_mutex, read, safe_path
from .frame_sequence import member_map_for_entry
from .lyrics import _font, timing_fingerprint, validate_lyrics
from .perf_scheduler import (COMPOSE_RECIPE, PERF_DIR, RUNTIME_CONTRACT,
                             _contiguous_ranges, _ffmpeg_version, _frame_key,
                             _load_state, _member_index_ranges, _op_ranges,
                             build_graph, graph_report)
from .transitions import _map_rows, audit_timeline

MANIFEST_TYPE = "render_manifest"

# The §11.5 dependency order the manifest records verbatim.
DEPENDENCY_ORDER = ["asset_bytes", "exposure", "cut_sequence",
                    "pairwise_transition", "clean_output",
                    "subtitle_overlay", "subbed_output", "encode_mux"]

ENCODE_ROLES = ("clean", "subbed")

# The declared change classes a rebuild plan can close over (design §11.2).
CHANGE_CLASSES = ("PICTURE", "SEQUENCE", "USED_RANGE", "TRANSITION",
                  "FONT", "CUE", "ENCODER", "LOCATOR",
                  "UNADOPTED_CANDIDATE", "REVIEW_MEMO", "PREPARATION",
                  "COLOR_TOOL")

# Field names that must never appear inside hashed content material
# (design §11.5 / schema §8): locators, runtime/job state, approval state
# and self digests are excluded from every content key.
FORBIDDEN_MATERIAL_KEYS = frozenset({
    "abs_path", "accepted", "approval", "approvals", "approved",
    "digest", "document_digest", "job", "job_state", "location",
    "locator", "manifest_digest", "path", "review", "review_refs",
    "reviewer", "self_digest", "signature", "state", "status"})

SCOPE_NOTES = [
    "GPU byte-level reproducibility is outside this slice; it is never claimed",
    "a generation re-call is not a replay — replay means re-running the "
    "deterministic compose from recorded content keys",
    "locator, runtime status, approval state and self digest are excluded "
    "from every content key",
    "synthetic local fixtures are development acceptance only — not artwork "
    "review, qualification or release evidence"]

_FACETS = {"qualification_state": "UNQUALIFIED",
           "acceptance_state": "PENDING",
           "release_state": "NOT_AUTHORIZED"}

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_INSTANCE_RE = re.compile(r"I[0-9]{3,5}")
_SHOT_RE = re.compile(r"S[0-9]{3,5}")
_ASSET_RE = re.compile(r"A[0-9]{4,6}")
_TRANSITION_RE = re.compile(r"T[0-9]{3,5}")
_WAVE_RE = re.compile(r"W[0-9]{2}")


# --- content-key plumbing -------------------------------------------------------

def _assert_material(value, where="content material"):
    """Refuse material carrying locator/status/approval/self-digest keys."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_MATERIAL_KEYS:
                raise FilmError(
                    f"{where}: {key!r} must not enter a content key — "
                    "locator, status, approval and self digest are excluded")
            _assert_material(child, where)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_material(child, where)


def content_digest(material):
    """sha256 over ANIM-001 canonical bytes of content-only material.

    canon_bytes pins integers, reduced rationals and ordered keys; floats,
    NaN and out-of-range integers raise here as everywhere else.
    """
    _assert_material(material)
    return hashlib.sha256(canon_bytes(material)).hexdigest()


def source_digest(entry, members):
    """Layer-1 `source_digest`: role, asset id, revision and the ordered
    member byte hashes of the adopted revision (schema §8). The member
    locator is transport metadata and never enters."""
    ordered = [{"index": index, "sha256": members[index]["sha256"]}
               for index in sorted(members)]
    return content_digest({
        "role": "cut_sequence_source",
        "instance_id": entry["instance_id"],
        "shot_id": entry["shot_id"],
        "asset_id": entry["pin"]["asset_id"],
        "revision": entry["pin"]["revision"],
        "members": ordered})


def toolchain_digest(ffmpeg_version=None):
    """The qualified-toolchain hash: the deterministic runtime contract and
    the exact tool versions this environment ran. An UNKNOWN ffmpeg probe
    is recorded honestly, never invented."""
    if ffmpeg_version is None:
        ffmpeg_version = _ffmpeg_version()
    import PIL
    return content_digest({
        "role": "qualified_toolchain",
        "runtime_contract": RUNTIME_CONTRACT,
        "compose_recipe": COMPOSE_RECIPE,
        "python": platform.python_version(),
        "pillow": PIL.__version__,
        "ffmpeg": ffmpeg_version})


_FRAME_SOURCE_KEYS = ("instance_id", "shot_id", "local_frame_index",
                      "sequence_revision", "weight")


def canonical_frame_map(rows):
    """The schema §9 frame_map projection of scheduler rows: ordered frames,
    ordered sources, reduced `[num, den]` weights — member shas and
    locators stay in the source layer."""
    canon = []
    for row in rows:
        canon.append({
            "frame_index": row["frame_index"],
            "file": row["file"],
            "operations": row["operations"],
            "output_sha256": row.get("output_sha256"),
            "review_refs": row.get("review_refs", []),
            "sources": [{key: source[key] for key in _FRAME_SOURCE_KEYS}
                        for source in row["sources"]]})
    return canon


def frame_map_digest(rows, *, complete=False):
    """Content digest of the frame_map: ordered frames, ordered sources,
    ordered reduced weights. `complete` also binds each frame's rendered
    output hash; the plan-level digest never does. Review references are
    approval data and never enter."""
    material = []
    for row in canonical_frame_map(rows):
        canon_row = {"file": row["file"],
                     "frame_index": row["frame_index"],
                     "operations": row["operations"],
                     "sources": row["sources"]}
        if complete:
            canon_row["output_sha256"] = row["output_sha256"]
        material.append(canon_row)
    return content_digest({"role": "frame_map", "rows": material})


# --- render_manifest document ----------------------------------------------------

_MANIFEST_FIELDS = frozenset({
    "document_type", "schema_version", "snapshot_digest", "edit_digest",
    "frame_map_digest", "source_digest", "sources", "recipe_digest",
    "toolchain_digest", "audio_sha256", "lyrics", "sequences", "encodes",
    "dependency_order", "frame_count"})
_SOURCE_FIELDS = frozenset({"asset_id", "instance_id", "member_count",
                            "revision", "shot_id", "source_digest"})
_LYRICS_FIELDS = frozenset({"font_sha256", "source_sha256", "timing_sha256"})
_SEQUENCE_FIELDS = frozenset({"frame_count", "rendered", "sequence_root"})
_ENCODE_FIELDS = frozenset({"artifact_sha256", "driver", "encode_digest"})


def _check_sha(value, field, *, allow_none=False):
    if value is None and allow_none:
        return
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise FilmError(
            f"render_manifest {field} must be a lowercase sha256")


def validate_render_manifest(document):
    """MANIFEST-CYCLE gate.

    The manifest's field set is exact: a self digest, an approval/status
    record or a locator smuggled inside is an unknown-field rejection, and
    non-canonical values (floats, NaN, out-of-range integers, duplicate
    keys on read) fail the ANIM-001 serializer check.
    """
    check_document(document, MANIFEST_TYPE)
    if not isinstance(document, dict) or set(document) != _MANIFEST_FIELDS:
        raise FilmError("render_manifest must contain exactly: "
                        + ", ".join(sorted(_MANIFEST_FIELDS)))
    canon_bytes(document)  # floats / NaN / non-int64 are rejections
    for field in ("snapshot_digest", "edit_digest", "frame_map_digest",
                  "source_digest", "recipe_digest", "toolchain_digest",
                  "audio_sha256"):
        _check_sha(document[field], field)
    if type(document["frame_count"]) is not int \
            or document["frame_count"] < 1:
        raise FilmError("render_manifest frame_count must be >= 1")
    if document["dependency_order"] != DEPENDENCY_ORDER:
        raise FilmError("render_manifest dependency_order is fixed")
    sources = document["sources"]
    if not isinstance(sources, list) or not sources:
        raise FilmError("render_manifest needs a non-empty sources list")
    seen = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
            raise FilmError("render_manifest source fields are fixed")
        if not _INSTANCE_RE.fullmatch(str(source["instance_id"])):
            raise FilmError("render_manifest source instance_id is malformed")
        if not _SHOT_RE.fullmatch(str(source["shot_id"])):
            raise FilmError("render_manifest source shot_id is malformed")
        if not _ASSET_RE.fullmatch(str(source["asset_id"])):
            raise FilmError("render_manifest source asset_id is malformed")
        if type(source["revision"]) is not int or source["revision"] < 1:
            raise FilmError("render_manifest source revision must be >= 1")
        if type(source["member_count"]) is not int \
                or source["member_count"] < 1:
            raise FilmError("render_manifest source member_count must be >= 1")
        _check_sha(source["source_digest"], "sources.source_digest")
        if source["instance_id"] in seen:
            raise FilmError("render_manifest lists an instance twice")
        seen.add(source["instance_id"])
    lyrics = document["lyrics"]
    if not isinstance(lyrics, dict) or set(lyrics) != _LYRICS_FIELDS:
        raise FilmError("render_manifest lyrics fields are fixed")
    for field in _LYRICS_FIELDS:
        _check_sha(lyrics[field], f"lyrics.{field}", allow_none=True)
    sequences = document["sequences"]
    if not isinstance(sequences, dict) or set(sequences) != set(ENCODE_ROLES):
        raise FilmError("render_manifest sequences needs clean and subbed")
    roots = set()
    for role in ENCODE_ROLES:
        block = sequences[role]
        if not isinstance(block, dict) or set(block) != _SEQUENCE_FIELDS:
            raise FilmError(f"render_manifest sequences.{role} fields are fixed")
        if type(block["rendered"]) is not bool \
                or type(block["frame_count"]) is not int \
                or block["frame_count"] < 0:
            raise FilmError(f"sequences.{role} counts are malformed")
        _check_sha(block["sequence_root"], f"sequences.{role}.sequence_root",
                   allow_none=True)
        if block["sequence_root"]:
            roots.add(block["sequence_root"])
    if len(roots) == 1:
        raise FilmError("clean and subbed must not share a sequence_root")
    encodes = document["encodes"]
    if not isinstance(encodes, dict) or set(encodes) != set(ENCODE_ROLES):
        raise FilmError("render_manifest encodes needs clean and subbed")
    for role in ENCODE_ROLES:
        record = encodes[role]
        if record is None:
            continue
        if not isinstance(record, dict) or set(record) != _ENCODE_FIELDS:
            raise FilmError(f"render_manifest encodes.{role} fields are fixed")
        _check_sha(record["encode_digest"], f"encodes.{role}.encode_digest")
        _check_sha(record["artifact_sha256"],
                   f"encodes.{role}.artifact_sha256", allow_none=True)
        if not isinstance(record["driver"], str) or not record["driver"]:
            raise FilmError(f"encodes.{role}.driver must be a driver name")
    return document


def manifest_digest(manifest):
    """The manifest's own digest — always computed *outside* the document,
    never stored inside it (a self digest is rejected by the validator)."""
    validate_render_manifest(manifest)
    return hashlib.sha256(canon_bytes(manifest)).hexdigest()


def _lyrics_layer(p, config):
    """The cue/font content inputs of the subbed layer: source text digest,
    timing fingerprint and font bytes hash — no approval or locator data."""
    analysis = read(safe_path(p, "analysis/audio.json"), {}) or {}
    duration_ms = analysis.get("duration_ms")
    layer = {"font_sha256": None, "source_sha256": None,
             "timing_sha256": None}
    if type(duration_ms) is not int or duration_ms <= 0:
        return layer
    document, _ = validate_lyrics(p, duration_ms, strict=False)
    text = "".join(cue["text"] for cue in document.get("cues", []))
    try:
        _, report = _font(p, config, text)
        layer["font_sha256"] = report.get("sha256")
    except FilmError:
        layer["font_sha256"] = None
    layer["source_sha256"] = document.get("source_sha256")
    layer["timing_sha256"] = timing_fingerprint(document)
    return layer


def build_manifest(project, *, state=None, exposure=None,
                   ffmpeg_version=None, graph=None):
    """The `render_manifest` document for the current inputs plus, when a
    committed perf state is supplied, its committed sequence roots and
    encode digests. Nothing is executed here; roots come from state.
    """
    p = Path(project)
    config = require_animation_profile(p)
    graph = graph or build_graph(p, exposure=exposure)
    sources = []
    for entry in graph["entries"]:
        table = graph["members"][entry["instance_id"]]
        sources.append({
            "asset_id": entry["pin"]["asset_id"],
            "instance_id": entry["instance_id"],
            "member_count": len(table),
            "revision": entry["pin"]["revision"],
            "shot_id": entry["shot_id"],
            "source_digest": source_digest(entry, table)})
    sequences = {}
    for role in ENCODE_ROLES:
        block = (state or {}).get(role) or {}
        frames = block.get("frames") or []
        sequences[role] = {"frame_count": len(frames),
                           "rendered": bool(frames),
                           "sequence_root": block.get("sequence_root")
                           if frames else None}
    encodes = {}
    for role in ENCODE_ROLES:
        record = (state or {}).get("encodes", {}).get(role)
        encodes[role] = None if record is None else {
            "artifact_sha256": record.get("artifact_sha256"),
            "driver": record.get("driver"),
            "encode_digest": record.get("encode_digest")}
    manifest = {
        "document_type": MANIFEST_TYPE,
        "schema_version": 1,
        "audio_sha256": graph["audio_sha256"],
        "dependency_order": list(DEPENDENCY_ORDER),
        "edit_digest": graph["timeline_sha256"],
        "encodes": encodes,
        "frame_count": graph["output_frames"],
        "frame_map_digest": frame_map_digest(graph["rows"]),
        "lyrics": _lyrics_layer(p, config),
        "recipe_digest": graph["recipe_digest"],
        "sequences": sequences,
        "snapshot_digest": graph["snapshot_digest"],
        "source_digest": content_digest({
            "role": "source_set",
            "sources": [{"instance_id": source["instance_id"],
                         "source_digest": source["source_digest"]}
                        for source in sources]}),
        "sources": sources,
        "toolchain_digest": toolchain_digest(ffmpeg_version)}
    return validate_render_manifest(manifest)


# --- change-closure rebuild plan ---------------------------------------------------

def _instance_for(graph, change):
    iid = change.get("instance_id")
    if iid is None and change.get("shot_id"):
        iid = next((e["instance_id"] for e in graph["entries"]
                    if e["shot_id"] == change["shot_id"]), None)
    known = {e["instance_id"] for e in graph["entries"]}
    if iid not in known:
        raise FilmError(f"change targets an unknown instance: {change}")
    return iid


def _normalize_changes(graph, changes):
    normalized = []
    for raw in changes:
        if not isinstance(raw, dict):
            raise FilmError("each rebuild-plan change must be an object")
        cls = raw.get("class")
        if cls not in CHANGE_CLASSES:
            raise FilmError(f"unknown change class {cls!r}; known classes: "
                            + ", ".join(CHANGE_CLASSES))
        change = dict(raw)
        change["class"] = cls
        if cls in ("PICTURE", "SEQUENCE", "USED_RANGE", "LOCATOR"):
            change["instance_id"] = _instance_for(graph, change)
        if cls == "TRANSITION":
            tid = change.get("transition_id")
            known = {t["id"] for t in graph["transitions"]}
            if tid not in known:
                raise FilmError(f"change targets an unknown transition: {tid}")
        if cls in ("PICTURE", "SEQUENCE"):
            members = change.get("member_indices")
            if members is None and change.get("member_index") is not None:
                members = [change["member_index"]]
            if cls == "PICTURE" and not members:
                raise FilmError(
                    "a PICTURE change names member_index/member_indices")
            if members is not None and (
                    not isinstance(members, list)
                    or any(type(m) is not int or m < 0 for m in members)):
                raise FilmError("member indices must be non-negative ints")
            change["member_indices"] = members  # None: whole adopted revision
        if cls == "ENCODER":
            roles = change.get("roles") or list(ENCODE_ROLES)
            if any(role not in ENCODE_ROLES for role in roles):
                raise FilmError("ENCODER roles are clean and/or subbed")
            change["roles"] = list(roles)
        normalized.append(change)
    return normalized


def _simulated(graph, pending, exposure, member_changed):
    """Re-plan the frame rows of a structurally edited timeline and mark
    declared-changed members (and members outside the pinned table) as
    changed — a conservative key set for the pending edit."""
    audit = audit_timeline(pending)
    entries = {e["instance_id"]: e for e in pending["entries"]}
    pins = {e["instance_id"]: e["pin"] for e in graph["entries"]}
    resolved = {}
    for row in audit["entries"]:
        iid = row["instance_id"]
        resolved[iid] = {
            "member_map": member_map_for_entry(
                entries[iid], (exposure or {}).get(iid)),
            "pin": pins[iid]}
    rows = _map_rows(audit, resolved)
    fmt = graph["format"]
    keys = []
    for row in rows:
        for source in row["sources"]:
            iid, index = source["instance_id"], source["local_frame_index"]
            meta = graph["members"].get(iid, {}).get(index)
            known = meta is not None \
                and index not in member_changed.get(iid, ())
            source["member_sha256"] = meta["sha256"] if known \
                else "changed-or-unknown-member"
        keys.append(_frame_key(row, fmt["width"], fmt["height"]))
    return audit, rows, keys


def _dirty_closure(rows, ops, transitions, dirty):
    """Members/halo/ranges/transition coverage for a dirty frame set —
    the same closure rule the ANIM-017 invalidation applies."""
    op_of = {}
    for op in ops:
        for frame in range(*op["output_range"]):
            op_of[frame] = op["instance_id"]
    needed, halo, affected = {}, {}, set()
    for frame in sorted(dirty):
        if frame < 0 or frame >= len(rows):
            continue
        owner = op_of.get(frame)
        for source in rows[frame]["sources"]:
            iid, index = source["instance_id"], source["local_frame_index"]
            needed.setdefault(iid, set()).add(index)
            affected.add(iid)
            if owner is not None and iid != owner:
                halo.setdefault(iid, set()).add(index)
    stale_transitions = {t["id"] for t in transitions
                         if dirty & set(range(*t["output_range"]))}
    return {"affected_instances": sorted(affected),
            "compose_ranges": _contiguous_ranges(sorted(dirty)),
            "dirty_frames": sorted(dirty),
            "halo_members": {k: sorted(v) for k, v in sorted(halo.items())},
            "needed_members": {k: sorted(v) for k, v in sorted(needed.items())},
            "stale_transitions": sorted(stale_transitions)}


def _read_plan(graph, closure):
    """The source/member/halo read plan: which member indices of each
    instance must be read, with their locators and byte lengths (locators
    are read-plan transport data, never content-key material)."""
    plan = {}
    for iid in sorted(set(closure["needed_members"])
                      | set(closure["halo_members"])):
        table = graph["members"][iid]
        halo = set(closure["halo_members"].get(iid, ()))
        members = []
        for index in sorted(closure["needed_members"].get(iid, ())):
            meta = table[index]
            members.append({"byte_length": meta["byte_length"],
                            "halo": index in halo,
                            "locator": meta["locator"],
                            "member_index": index})
        plan[iid] = {"member_count": len(members), "members": members}
    return plan


def _stale_projection(graph, *, member_changed, range_changed,
                      transition_edited, pixel_stale_transitions,
                      structure_changed, subbed_changed, encode_roles,
                      output_spec_changed, recipe_changed, intent_changed,
                      dirty):
    """Which approval scopes the declared changes make stale — projected at
    the same granularity the review/lock bindings actually hash, so the
    projection matches `review_status`/`lock_status` after the change is
    applied. Unbound scopes stay KEPT and say why."""
    cuts = {}
    for entry in graph["entries"]:
        iid = entry["instance_id"]
        reasons = []
        if member_changed.get(iid):
            reasons.append("adopted member bytes changed")
        if iid in range_changed:
            reasons.append("used source range/handles changed")
        if recipe_changed:
            reasons.append("shared render recipe/toolchain changed")
        cuts[iid] = {"state": "STALE" if reasons else "KEPT",
                     "reason": "; ".join(reasons) or "bound fields unchanged"}
    transitions = {}
    for transition in graph["transitions"]:
        reasons = []
        if transition["id"] in transition_edited:
            reasons.append("transition fields changed")
        for end in ("from_instance", "to_instance"):
            if member_changed.get(transition[end]):
                reasons.append(f"{transition[end]} sequence bytes changed")
            if transition[end] in range_changed:
                reasons.append(f"{transition[end]} used range changed")
        if transition["id"] in pixel_stale_transitions:
            reasons.append("shared output range recomposed")
        if recipe_changed:
            reasons.append("recipe/toolchain changed")
        transitions[transition["id"]] = {
            "state": "STALE" if reasons else "KEPT",
            "reason": "; ".join(reasons) or "bound fields unchanged"}
    locks = {}
    plan_reasons = []
    if structure_changed:
        plan_reasons.append("edit structure changed")
    if output_spec_changed:
        plan_reasons.append("output spec changed")
    if intent_changed:
        plan_reasons.append("shared intent changed")
    locks["PLAN_LOCK"] = {"state": "STALE" if plan_reasons else "KEPT",
                          "reason": "; ".join(plan_reasons)
                          or "plan binding unchanged"}
    changed_shots = {e["shot_id"] for e in graph["entries"]
                     if member_changed.get(e["instance_id"])
                     or e["instance_id"] in range_changed}
    waves = load_waves(Path(graph["project"]))
    if waves:
        for wave in waves["waves"]:
            hit = sorted(set(wave["shots"]) & changed_shots)
            locks[f"WAVE_LOCK:{wave['wave']}"] = {
                "state": "STALE" if hit else "KEPT",
                "reason": ("adopted cuts changed: " + ", ".join(hit))
                if hit else "wave cuts unchanged"}
    final_reasons = []
    if structure_changed:
        final_reasons.append("edit digest changed")
    if output_spec_changed:
        final_reasons.append("output spec changed")
    if subbed_changed:
        final_reasons.append("lyrics/font binding changed")
    if any(c["state"] == "STALE" for c in cuts.values()) \
            or any(t["state"] == "STALE" for t in transitions.values()):
        final_reasons.append("bound review set changed")
    locks["FINAL_LOCK"] = {"state": "STALE" if final_reasons else "KEPT",
                           "reason": "; ".join(final_reasons)
                           or "final binding unchanged"}
    film_reasons = []
    if dirty:
        film_reasons.append("frame sequence roots changed")
    if subbed_changed:
        film_reasons.append("subbed sequence/font/lyrics changed")
    if encode_roles:
        film_reasons.append("deliverable must be re-encoded")
    if structure_changed:
        film_reasons.append("edit digest changed")
    final_film = {"state": "STALE" if film_reasons else "KEPT",
                  "reason": "; ".join(film_reasons)
                  or "deliverable binding unchanged"}
    stale, kept = [], []
    scopes = {**{f"CUT:{k}": v for k, v in cuts.items()},
              **{f"TRANSITION:{k}": v for k, v in transitions.items()},
              **locks, "FINAL_FILM": final_film}
    for name, scope in sorted(scopes.items()):
        (stale if scope["state"] == "STALE" else kept).append(name)
    return {"cuts": cuts, "transitions": transitions, "locks": locks,
            "final_film": final_film, "stale": stale, "kept": kept,
            "note": "projection of the declared changes at binding "
                    "granularity; actual staleness is recomputed by "
                    "review_status/lock_status after the change lands"}


def plan_rebuild(project, changes, *, exposure=None, graph=None):
    """The change-closure rebuild plan for a declared change set.

    Maps each declared change to the actual recompute scope — dirty output
    frames, needed members and halo, the source/member/halo read plan,
    subbed and encode scope — plus the stale-approval projection. Nothing
    is written and nothing executes; applying the change and running the
    pipeline produces the executed scope to compare against.
    """
    p = Path(project)
    require_animation_profile(p)
    graph = graph or build_graph(p, exposure=exposure)
    document = load_animation_timeline(p)
    changes = _normalize_changes(graph, list(changes))
    rows, ops, transitions = graph["rows"], graph["ops"], graph["transitions"]

    dirty = set()
    notes = []
    member_changed = {}      # instance_id -> declared changed member indices
    range_changed = set()    # instances whose used range/handles change
    transition_edited = set()
    structure_changed = False
    subbed_changed = False
    encode_roles = set()
    output_spec_changed = False
    recipe_changed = False
    intent_changed = False
    pending = copy.deepcopy(document)
    pending_entries = {e["instance_id"]: e for e in pending["entries"]}

    for change in changes:
        cls = change["class"]
        if cls in ("PICTURE", "SEQUENCE"):
            iid = change["instance_id"]
            table = graph["members"][iid]
            members = change.get("member_indices")
            if members is None:
                members = sorted(table)
                notes.append(f"{iid}: whole adopted revision declared changed")
            for index in members:
                if index not in table:
                    raise FilmError(
                        f"{iid} member {index} is not in the adopted revision")
                member_changed.setdefault(iid, set()).add(index)
        elif cls == "USED_RANGE":
            iid = change["instance_id"]
            entry = pending_entries[iid]
            rng = change.get("used_source_range")
            if rng is not None:
                if not isinstance(rng, list) or len(rng) != 2 \
                        or any(type(v) is not int for v in rng):
                    raise FilmError("used_source_range must be [start, end) "
                                    "integers")
                entry["used_source_range"] = list(rng)
            if "unused_handles" in change:
                handles = change["unused_handles"]
                if not isinstance(handles, dict) \
                        or set(handles) != {"before", "after"}:
                    raise FilmError("unused_handles must carry before/after")
                entry["unused_handles"] = dict(handles)
            range_changed.add(iid)
            structure_changed = True
        elif cls == "TRANSITION":
            tid = change["transition_id"]
            target = next(e for e in pending["entries"]
                          if (e.get("transition_out") or {}).get("id") == tid)
            updates = change.get("set") or {}
            allowed = {"type", "overlap_frames", "curve"}
            if set(updates) - allowed:
                raise FilmError("TRANSITION updates are limited to "
                                + ", ".join(sorted(allowed)))
            target["transition_out"] = {**target["transition_out"], **updates}
            transition_edited.add(tid)
            structure_changed = True
        elif cls in ("FONT", "CUE"):
            subbed_changed = True
            notes.append(f"{cls}: subbed layer and deliverable change; "
                         "clean frames and cut reviews are unaffected")
        elif cls == "ENCODER":
            encode_roles.update(change["roles"])
            if change.get("output_spec_changed"):
                output_spec_changed = True
                notes.append("ENCODER: output spec changed — plan/final "
                             "locks re-bind")
            else:
                notes.append("ENCODER: PNG sequences are reusable; a new "
                             "MP4 plus final approval is required")
        elif cls == "COLOR_TOOL":
            recipe_changed = True
            intent_changed = bool(change.get("intent_changed"))
            notes.append("COLOR_TOOL: every composed frame re-binds to the "
                         "new recipe/toolchain")
        elif cls == "LOCATOR":
            iid = change.get("instance_id") or change.get("shot_id")
            notes.append(f"LOCATOR ({iid}): identical bytes at a new "
                         "locator — read plan only, no content key moves")
        elif cls == "UNADOPTED_CANDIDATE":
            notes.append("an unadopted candidate stays outside the adopted "
                         "render closure — nothing is invalidated")
        elif cls in ("REVIEW_MEMO", "PREPARATION"):
            notes.append(f"{cls}: metadata only — nothing is invalidated")

    sim_audit, sim_rows = None, None
    if structure_changed:
        lengths = [e["used_source_range"][1] - e["used_source_range"][0]
                   for e in pending["entries"]]
        overlaps = [(e.get("transition_out") or {}).get("overlap_frames", 0)
                    for e in pending["entries"][:-1]]
        pending["target_frames"] = sum(lengths) - sum(overlaps)
        sim_audit, sim_rows, sim_keys = _simulated(
            graph, pending, exposure, member_changed)
        sim_ops = _op_ranges(sim_audit)
        old_keys = [row["key"] for row in rows]
        if len(sim_keys) != len(old_keys):
            # The scheduler's invalidation treats an output-length change
            # as a full-map rebuild — the plan reports the same closure.
            dirty.update(range(len(sim_keys)))
            notes.append(f"output length {len(old_keys)} -> {len(sim_keys)} "
                         "frames; a length change rebuilds the whole map")
        else:
            for index, key in enumerate(sim_keys):
                if key != old_keys[index]:
                    dirty.add(index)
        plan_rows, plan_ops, plan_transitions = \
            sim_rows, sim_ops, sim_audit["transitions"]
    else:
        for row in rows:
            if any(source["local_frame_index"]
                   in member_changed.get(source["instance_id"], ())
                   for source in row["sources"]):
                dirty.add(row["frame_index"])
        plan_rows, plan_ops, plan_transitions = rows, ops, transitions

    if recipe_changed:
        dirty.update(range(len(plan_rows)))

    closure = _dirty_closure(plan_rows, plan_ops, plan_transitions, dirty)
    closure["unchanged"] = not dirty
    closure["output_frames"] = {"before": len(rows),
                                "after": len(plan_rows)}
    read_plan = _read_plan(graph, closure) if not structure_changed \
        else _sim_read_plan(graph, closure)

    subbed_frames = set(range(len(plan_rows))) if subbed_changed \
        else set(dirty)
    scope_encode = set(encode_roles)
    if dirty or recipe_changed:
        scope_encode.add("clean")
        scope_encode.add("subbed")
    if subbed_changed:
        scope_encode.add("subbed")

    projection = _stale_projection(
        graph, member_changed=member_changed, range_changed=range_changed,
        transition_edited=transition_edited,
        pixel_stale_transitions=set(closure["stale_transitions"]),
        structure_changed=structure_changed,
        subbed_changed=subbed_changed, encode_roles=scope_encode,
        output_spec_changed=output_spec_changed,
        recipe_changed=recipe_changed, intent_changed=intent_changed,
        dirty=dirty)

    closure["subbed_dirty_frames"] = sorted(subbed_frames)
    closure["subbed_ranges"] = _contiguous_ranges(sorted(subbed_frames))
    closure["encode_roles"] = sorted(scope_encode)
    closure["decode_ranges"] = {
        iid: _member_index_ranges(closure["needed_members"][iid])
        for iid in sorted(closure["needed_members"])}

    return {"kind": "rebuild_plan",
            "project": str(p),
            "snapshot_digest": graph["snapshot_digest"],
            "changes": changes,
            "closure": closure,
            "read_plan": read_plan,
            "stale_approval_projection": projection,
            "notes": notes,
            "scope_notes": list(SCOPE_NOTES),
            "facets": dict(_FACETS)}


def _sim_read_plan(graph, closure):
    """Read plan after a structural edit: member indices are resolved on
    the simulated map; locators come from the current member table where
    the member is still pinned."""
    plan = {}
    for iid in sorted(set(closure["needed_members"])
                      | set(closure["halo_members"])):
        table = graph["members"].get(iid, {})
        halo = set(closure["halo_members"].get(iid, ()))
        members = []
        for index in sorted(closure["needed_members"].get(iid, ())):
            meta = table.get(index)
            members.append({
                "byte_length": meta["byte_length"] if meta else None,
                "halo": index in halo,
                "locator": meta["locator"] if meta else None,
                "member_index": index})
        plan[iid] = {"member_count": len(members), "members": members}
    return plan


# --- reports -------------------------------------------------------------------------

def _current_stale(reviews, locks):
    """The stale-approval projection of the *current* project state, in the
    same scope vocabulary the rebuild plan projects. FINAL_FILM binds per
    sealed build — it is reported separately, not folded in here."""
    stale, current, pending = [], [], []
    for target, entry in reviews["targets"].items():
        name = f"{entry['scope']}:{target}"
        state = entry["state"]
        (stale if state == "STALE"
         else current if state == "CURRENT" else pending).append(name)
    (stale if locks["plan"]["state"] == "STALE" else
     current if locks["plan"]["state"] == "CURRENT"
     else pending).append("PLAN_LOCK")
    for wave, entry in locks["waves"].items():
        (stale if entry["state"] == "STALE" else
         current if entry["state"] == "CURRENT"
         else pending).append(f"WAVE_LOCK:{wave}")
    (stale if locks["final"]["state"] == "STALE" else
     current if locks["final"]["state"] == "CURRENT"
     else pending).append("FINAL_LOCK")
    return {"stale": sorted(stale), "current": sorted(current),
            "pending": sorted(pending)}


def provenance_report(project, *, perf_root=None, exposure=None):
    """The provenance report: the canonical render_manifest (with committed
    roots/encodes when a perf state exists), the dependency-graph summary
    and the current stale-approval projection."""
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        root = Path(perf_root) if perf_root else safe_path(p, PERF_DIR)
        state = _load_state(root)
        graph = build_graph(p, exposure=exposure)
        manifest = build_manifest(p, state=state, exposure=exposure,
                                  graph=graph)
        reviews = review_status(p, strict=False)
        locks = lock_status(p)
        return {"kind": "render_provenance_report",
                "project": str(p),
                "manifest": manifest,
                "manifest_digest": manifest_digest(manifest),
                "graph": graph_report(graph),
                "render_state": "COMMITTED" if state else
                                "PENDING_NO_RENDER_STATE",
                "stale_approval_projection": _current_stale(reviews, locks),
                "reviews": reviews,
                "locks": locks,
                "scope_notes": list(SCOPE_NOTES),
                "facets": dict(_FACETS)}


def rebuild_plan_report(project, changes, *, exposure=None):
    """CLI payload for `rebuild-plan PROJECT --changes FILE`."""
    p = Path(project)
    with project_mutex(p):
        return plan_rebuild(p, changes, exposure=exposure)


# --- cold/warm comparison -------------------------------------------------------------

def actual_scope(result):
    """The executed read/decode/compose/encode scope of a run — the actual
    numbers a rebuild plan is checked against (REBUILD-CLOSURE)."""
    counters = result["metrics"]["counters"]
    invalidation = result["invalidation"]
    return {"affected_instances": invalidation["affected_instances"],
            "compose_ranges": _contiguous_ranges(invalidation["dirty_frames"]),
            "dirty_frames": invalidation["dirty_frames"],
            "encode_cache_hits": counters.get("encode_cache_hits", 0),
            "encode_runs": counters.get("encode_runs", 0),
            "frame_cache_hits": counters.get("frame_cache_hits", 0),
            "frame_composed": counters.get("frame_composed", 0),
            "halo_members": invalidation["halo_members"],
            "member_decodes": counters.get("member_decodes", 0),
            "member_reads": counters.get("member_fetches", 0),
            "needed_members": invalidation["needed_members"],
            "stale_transitions": invalidation["stale_transitions"],
            "subbed_burn_ranges": counters.get("subbed_burn_ranges", 0),
            "subbed_cache_hits": counters.get("subbed_cache_hits", 0),
            "subbed_dirty": counters.get("subbed_dirty", 0),
            "unchanged": invalidation["unchanged"]}


def compare_runs(warm, cold, *, rows=None):
    """REBUILD-COLD-WARM: with identical environment, input and quality a
    warm run and a fresh cold run must produce matching impact hashes
    (snapshot, clean/subbed sequence roots) and an identical frame_map.
    `rows` optionally supplies (warm_rows, cold_rows) graphs for the
    frame_map check; run results compare roots only."""
    checks = {"clean_sequence_root":
              warm["clean_sequence_root"] == cold["clean_sequence_root"],
              "snapshot_digest":
              warm["snapshot_digest"] == cold["snapshot_digest"],
              "subbed_sequence_root":
              warm["subbed_sequence_root"] == cold["subbed_sequence_root"]}
    if rows is not None:
        warm_rows, cold_rows = rows
        checks["frame_map"] = canonical_frame_map(warm_rows) == \
            canonical_frame_map(cold_rows)
        checks["frame_map_digest"] = frame_map_digest(warm_rows) == \
            frame_map_digest(cold_rows)
    return {"checks": checks, "equal": all(checks.values())}
