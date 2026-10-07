"""film-asset-library: asset read model and manifest-linked actions.

A read model over the ANIM-003 registry (`manifest/animation_assets.json`)
that answers which image is used by which cut and which current or
historical build, plus three actions linked to the current manifest:
replace (open a new revision), detach (drop a cut's assignment pin) and
cleanup of unused revisions.

Identity is content. An asset revision is its
(asset_id, revision, content_sha256) triple — never its filename. Two
members that share a source file name but hash differently are different
content; the view reports such name collisions explicitly instead of
merging them. Stored member files are the real originals: the library
labels every recorded member ORIGINAL, flags files inside a revision
directory that no record pins as UNRECORDED, and treats any thumbnail the
UI draws as a derived preview — never the original and never an approval.

Rights are not hashes. Member hash verification proves byte integrity
only: every revision's rights check is UNVERIFIED, and no license,
permission or artwork approval is derived from a hash match. The view
carries the honest facets (qualification UNQUALIFIED, acceptance PENDING,
release NOT_AUTHORIZED).

Cleanup is dry-run first. A revision is deletable only when nothing pins
it: the current manifest assignments, another asset's pin fields (mask
targets, replacement targets, rig layers, control references, dependency
lists), draft ledgers, work packets, review records, route decisions,
scope-lock bindings and every current or historical build's resolved
pins. A sealed build's snapshot registry preserves its own copy of unused
records, so only the revisions the build actually resolved or adopted
block deletion. Recomputing evidence happens inside the project mutex on
execute, so a revision that gained a reference is never removed.

LEGACY_MV projects do not enter this module at all; every entry point
requires the FRAME_ANIMATION_V1 profile.
"""
from pathlib import Path, PurePosixPath
import json
import os

from .animation_assets import (ASSET_ROOT, SEQUENCE_KINDS,
                               import_draft_image,
                               import_frame_sequence, import_layer_rgba,
                               import_mask, import_replacement_drawing,
                               import_rig_spec, load_registry, save_registry)
from .animation_review import sequence_content_digest
from .animation_schema import (SHOT_ID, load_animation_timeline, read_canon,
                               require_animation_profile)
from .builds import list_builds
from .core import FilmError, digest, now, project_mutex, read, safe_path

LOCKS_PATH = "manifest/animation_locks.json"
APPROVALS_PATH = "production/approvals.jsonl"
ROUTES_PATH = "production/route_decisions.jsonl"
DRAFTS_GLOB = "animation/shots/*/draft_frames.json"
PACKETS_GLOB = "animation/packets/*.json"

RIGHTS_UNVERIFIED = {"state": "UNVERIFIED",
                     "note": "member hash verification proves stored bytes, "
                             "not a license, permission or artwork approval"}
FACETS = {"qualification_state": "UNQUALIFIED",
          "acceptance_state": "PENDING",
          "release_state": "NOT_AUTHORIZED"}

PIN_KEYS = {"asset_id", "revision", "content_sha256"}


def _json_lines(path):
    """Parse a canonical JSONL file; a malformed line fails the scan."""
    lines = path.read_bytes().split(b"\n")
    records = []
    for index, line in enumerate(lines):
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise FilmError(f"{path.name} line {index + 1} is not JSON") from e
    return records


def _walk_pins(node, out):
    """Collect every dict that carries an asset pin shape."""
    if isinstance(node, dict):
        if {"asset_id", "revision"} <= set(node.keys()):
            out.append(node)
        for value in node.values():
            _walk_pins(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_pins(value, out)


def _collect_refs(p, registry, *, errors):
    """Every place a revision pin or bare content hash is recorded.

    Returns (pin_refs, content_refs, shot_assets):
    - pin_refs: {(asset_id, revision): {"reasons": set, "uses": list}}
    - content_refs: {content_sha256: {"reasons": set, "uses": list}} for
      evidence that names content without an asset id (route decisions)
    - shot_assets: {shot_id: {asset_id}} the evidence chain links to a cut

    `errors` is a list to append human-readable scan warnings to; when it
    is None the first unreadable evidence document raises instead.
    """
    pin_refs, content_refs, shot_assets = {}, {}, {}

    def fail(message):
        if errors is None:
            raise FilmError(message)
        errors.append(message)

    def pin_ref(pin, reason, use=None):
        if not {"asset_id", "revision"} <= set(pin.keys()):
            return
        key = (pin["asset_id"], int(pin["revision"]))
        entry = pin_refs.setdefault(key, {"reasons": set(), "uses": []})
        entry["reasons"].add(reason)
        if use is not None:
            entry["uses"].append(dict(use, reason=reason))

    def content_ref(sha, reason, use=None):
        if type(sha) is not str:
            return
        entry = content_refs.setdefault(sha, {"reasons": set(), "uses": []})
        entry["reasons"].add(reason)
        if use is not None:
            entry["uses"].append(dict(use, reason=reason))

    def link_shot(shot_id, asset_id):
        if type(shot_id) is str and type(asset_id) is str:
            shot_assets.setdefault(shot_id, set()).add(asset_id)

    try:
        timeline = load_animation_timeline(p)
    except FilmError as exc:
        timeline = {"entries": []}
        fail(f"timeline unreadable; instance links may be missing: {exc}")
    instances = {}
    for entry in timeline["entries"]:
        instances.setdefault(entry["shot_id"], []).append(entry["instance_id"])
    instance_shot = {e["instance_id"]: e["shot_id"]
                     for e in timeline["entries"]}

    # 1. The current manifest assignments — the authoritative "used in".
    for shot, pin in registry["assignments"].items():
        for instance in instances.get(shot, [None]):
            pin_ref(pin, f"assigned to {shot} (current manifest)",
                    use={"shot_id": shot, "instance_id": instance,
                         "where": "current manifest"})
        link_shot(shot, pin["asset_id"])

    # 2. Asset-to-asset pins inside the registry itself.
    for aid, entry in registry["assets"].items():
        for label, record in entry["revisions"].items():
            where = f"{aid} r{record['revision']} ({record['kind']})"
            for pin in record.get("dependencies", []):
                pin_ref(pin, f"dependency of {where}")
            if record["kind"] == "MASK":
                pin_ref(record["target"], f"MASK target of {where}")
            elif record["kind"] == "REPLACEMENT_DRAWING":
                pin_ref(record["replaces"], f"replaced by {where}")
            elif record["kind"] == "CONTROL_IMAGE":
                for pin in record["references"]:
                    pin_ref(pin, f"CONTROL_IMAGE reference of {where}")
            elif record["kind"] == "RIG_SPEC":
                for layer in record["spec"]["layers"]:
                    pin_ref(layer["asset"],
                            f"rig layer {layer['id']} of {where}")

    # 3. Scope locks: WAVE_LOCK bindings carry explicit cut pins.
    try:
        path = safe_path(p, LOCKS_PATH)
        if path.is_file():
            for record in read_canon(path).get("locks", []):
                label = f"{record['scope']} {record['lock_id']}" + (
                    f" {record['wave']}" if record.get("wave") else "")
                for cut in (record.get("binding") or {}).get("cuts", []):
                    pin_ref(cut, label, use={
                        "shot_id": cut.get("shot_id"),
                        "instance_id": cut.get("instance_id"),
                        "where": label})
                    link_shot(cut.get("shot_id"), cut.get("asset_id"))
    except (FilmError, OSError) as exc:
        fail(f"{LOCKS_PATH} unreadable; lock protection incomplete: {exc}")

    # 4. Route decisions: cut records pin content, not asset ids.
    try:
        path = safe_path(p, ROUTES_PATH)
        if path.is_file():
            for record in _json_lines(path):
                decision = record.get("decision_id", "route decision")
                for cut in record.get("cuts", []):
                    content_ref(cut.get("content_sha256"), decision,
                                use={"shot_id": cut.get("shot_id"),
                                     "instance_id": cut.get("instance_id"),
                                     "where": decision})
    except (FilmError, OSError) as exc:
        fail(f"{ROUTES_PATH} unreadable; decision protection incomplete: {exc}")

    # 5. Review records: CUT binds asset_id+revision; TRANSITION binds the
    # endpoint revision numbers without an asset id (second pass below) and
    # each endpoint's sequence digest (third pass).
    transition_refs, transition_digests = [], []
    try:
        path = safe_path(p, APPROVALS_PATH)
        if path.is_file():
            for record in _json_lines(path):
                label = f"review {record.get('review_id')} " \
                        f"({record.get('decision')})"
                if record.get("scope") == "CUT":
                    pin_ref(record, label, use={
                        "shot_id": record.get("shot_id"),
                        "instance_id": record.get("instance_id"),
                        "where": label})
                    link_shot(record.get("shot_id"), record.get("asset_id"))
                elif record.get("scope") == "TRANSITION":
                    for side in ("from", "to"):
                        transition_refs.append(
                            (record.get(f"{side}_instance"),
                             record.get(f"{side}_revision"), label))
                        transition_digests.append(
                            (record.get(f"{side}_sequence_digest"), label))
    except (FilmError, OSError) as exc:
        fail(f"{APPROVALS_PATH} unreadable; review protection incomplete: {exc}")

    # 6. Builds: resolved entry pins and the snapshot registry assignments —
    #    the revisions the build actually adopted. Unused records in the
    #    snapshot registry are preserved inside the build itself, so they
    #    are reported but do not block deletion.
    try:
        for build in list_builds(p):
            build_id = build["build_id"]
            mode = build.get("mode") or build.get("document_type") or "Build 1"
            label = f"build {build_id} ({mode})"
            for entry in build.get("entries") or []:
                if entry.get("resolved") is False:
                    continue
                pin_ref(entry, label, use={
                    "shot_id": entry.get("shot_id"),
                    "instance_id": entry.get("instance_id"),
                    "where": label})
                link_shot(entry.get("shot_id"), entry.get("asset_id"))
            snapshot = Path(build["build_dir"]) / "snapshot" / \
                "manifest" / "animation_assets.json"
            if snapshot.is_file():
                try:
                    document = read_canon(snapshot)
                except FilmError as exc:
                    fail(f"{build_id} snapshot registry unreadable: {exc}")
                    continue
                for shot, pin in (document.get("assignments") or {}).items():
                    pin_ref(pin, f"{label} snapshot assignment",
                            use={"shot_id": shot,
                                 "instance_id": next(
                                     iter(instances.get(shot, [None]))),
                                 "where": label})
                    link_shot(shot, pin["asset_id"])
    except (FilmError, OSError) as exc:
        fail(f"build history unreadable; build protection incomplete: {exc}")

    # 7. Per-shot draft frame ledgers.
    try:
        for ledger in sorted(Path(p).glob(DRAFTS_GLOB)):
            document = read_canon(ledger)
            label = f"draft ledger {document['shot_id']}"
            for entry in document.get("entries", []):
                if entry.get("asset_pin"):
                    pin_ref(entry["asset_pin"], label,
                            use={"shot_id": document["shot_id"],
                                 "instance_id": next(iter(
                                     instances.get(document["shot_id"],
                                                   [None]))),
                                 "where": label})
                    link_shot(document["shot_id"],
                              entry["asset_pin"]["asset_id"])
                for pin in entry.get("references", []):
                    pin_ref(pin, label)
    except (FilmError, OSError) as exc:
        fail(f"draft ledgers unreadable; ledger protection incomplete: {exc}")

    # 8. Work packets carry the input pins a hand-off was conditioned on.
    try:
        for packet in sorted(Path(p).glob(PACKETS_GLOB)):
            pins = []
            _walk_pins(read_canon(packet), pins)
            for pin in pins:
                pin_ref(pin, f"work packet {packet.stem}")
    except (FilmError, OSError) as exc:
        fail(f"work packets unreadable; packet protection incomplete: {exc}")

    # Second pass: transition reviews name an instance and a bare revision.
    for instance, revision, label in transition_refs:
        if not isinstance(revision, int):
            continue
        for asset_id in shot_assets.get(instance_shot.get(instance), ()):
            entry = registry["assets"].get(asset_id)
            if entry is not None and str(revision) in entry["revisions"]:
                pin_ref({"asset_id": asset_id, "revision": revision,
                         "content_sha256":
                             entry["revisions"][str(revision)]
                             ["content_sha256"]},
                        f"{label} (transition endpoint)")

    # Third pass: a transition review also binds each endpoint's sequence
    # digest — match it against every registered sequence revision so the
    # revision a review bound stays protected even when the bare revision
    # number can no longer be linked back to an asset through the cut.
    if transition_digests:
        bound = {}
        for asset_id, entry in registry["assets"].items():
            for record in entry["revisions"].values():
                if record["kind"] in SEQUENCE_KINDS:
                    bound.setdefault(
                        sequence_content_digest(record), []).append(
                            (asset_id, record))
        for sha, label in transition_digests:
            if type(sha) is not str:
                continue
            for asset_id, record in bound.get(sha, ()):
                pin_ref({"asset_id": asset_id,
                         "revision": record["revision"],
                         "content_sha256": record["content_sha256"]},
                        f"{label} (transition sequence digest)")

    return pin_refs, content_refs, shot_assets


def _refs_for(pin_refs, content_refs, asset_id, record):
    """The reasons and uses that pin one revision."""
    merged = {"reasons": set(), "uses": []}
    for source in (pin_refs.get((asset_id, record["revision"])),
                   content_refs.get(record["content_sha256"])):
        if source is None:
            continue
        merged["reasons"] |= source["reasons"]
        merged["uses"] += source["uses"]
    return merged


def _member_files(p, asset_id, record, verify_bytes):
    """Per-member integrity: ORIGINAL for recorded members, UNRECORDED for
    files the registry never pinned inside the revision directory."""
    files, integrity = [], "VERIFIED"
    recorded = set()
    for member in record["files"]:
        relative = member["relative_name"]
        recorded.add(relative)
        status = "VERIFIED"
        if verify_bytes:
            path = safe_path(p, relative)
            if path.is_symlink() or not path.is_file():
                status = "MISSING"
            elif path.stat().st_size != member["byte_length"]:
                status = "LENGTH_MISMATCH"
            elif digest(path) != member["sha256"]:
                status = "HASH_MISMATCH"
        else:
            if not safe_path(p, relative).is_file():
                status = "MISSING"
        if status == "MISSING":
            integrity = "MISSING"
        elif status != "VERIFIED" and integrity != "MISSING":
            integrity = "CORRUPT"
        files.append({"relative_name": relative,
                      "sha256": member["sha256"],
                      "byte_length": member["byte_length"],
                      "frame_index": member.get("frame_index"),
                      "source_name": member.get("source_name"),
                      "role": "ORIGINAL", "status": status})
    revision_dir = safe_path(p, f"{ASSET_ROOT}/{asset_id}/r{record['revision']}")
    if revision_dir.is_dir():
        for found in sorted(revision_dir.rglob("*")):
            if not found.is_file() or found.is_symlink():
                continue
            relative = f"{ASSET_ROOT}/{asset_id}/r{record['revision']}/" \
                + "/".join(found.relative_to(revision_dir).parts)
            if relative not in recorded:
                files.append({"relative_name": relative, "sha256": None,
                              "byte_length": found.stat().st_size,
                              "frame_index": None, "source_name": None,
                              "role": "UNRECORDED", "status": "UNRECORDED"})
    return files, integrity


def library_view(project, *, kind=None, revision=None, origin=None,
                 rights=None, shot_id=None, verify_bytes=True):
    """The asset library read model: kinds, revisions, origins, rights and
    where each revision is used — current manifest first, then the
    historical evidence chain.

    Filters AND together: `kind` (a registry kind name), `revision`,
    `origin` (a provenance type such as EXTERNAL_IMPORT), `rights` (only
    UNVERIFIED ever exists — any other value matches nothing) and
    `shot_id` (revisions used in that cut by any evidence). `verify_bytes`
    rehashes every member; set it False to skip hashing and report only
    presence.
    """
    p = Path(project)
    require_animation_profile(p)
    registry = load_registry(p)
    warnings = []
    pin_refs, content_refs, _ = _collect_refs(p, registry, errors=warnings)
    assets, name_map = [], {}
    for asset_id in sorted(registry["assets"]):
        entry = registry["assets"][asset_id]
        revisions, used_in, builds, worst = [], [], set(), "VERIFIED"
        for label in sorted(entry["revisions"], key=int):
            record = entry["revisions"][label]
            refs = _refs_for(pin_refs, content_refs, asset_id, record)
            files, integrity = _member_files(p, asset_id, record, verify_bytes)
            if integrity == "MISSING" or (integrity == "CORRUPT"
                                        and worst == "VERIFIED"):
                worst = integrity
            uses = [dict(u) for u in refs["uses"]
                    if u.get("shot_id") or u.get("instance_id")]
            for use in uses:
                if use.get("where", "").startswith("build "):
                    builds.add(use["where"].split(" ")[1])
            for member in record["files"]:
                name = member.get("source_name")
                if name:
                    name_map.setdefault(name, []).append(
                        {"asset_id": asset_id, "revision": record["revision"],
                         "sha256": member["sha256"]})
            revisions.append({
                "revision": record["revision"],
                "content_sha256": record["content_sha256"],
                "kind": record["kind"],
                "acceptance": record["acceptance"]["state"],
                "preparation": record["preparation"].get("state"),
                "not_verified": record["preparation"].get("not_verified", []),
                "origin": record["provenance"].get("type"),
                "provenance": record["provenance"],
                "members": len(record["files"]),
                "bytes": sum(f["byte_length"] for f in record["files"]),
                "files": files, "integrity": integrity,
                "current": record["revision"] == entry["current_revision"],
                "used_in": uses, "held_by": sorted(refs["reasons"]),
                "protected": bool(refs["reasons"]),
                "source_names": [m.get("source_name")
                                 for m in record["files"]
                                 if m.get("source_name")],
                "rights": dict(RIGHTS_UNVERIFIED)})
            used_in += uses
        current = entry["revisions"][str(entry["current_revision"])]
        assets.append({"asset_id": asset_id, "kind": current["kind"],
                       "current_revision": entry["current_revision"],
                       "revisions": revisions,
                       "revisions_count": len(revisions),
                       "acceptance": current["acceptance"]["state"],
                       "rights": dict(RIGHTS_UNVERIFIED),
                       "used_in": used_in, "builds": sorted(builds),
                       "integrity": worst})
    collisions = [{"source_name": name, "holders": holders}
                  for name, holders in sorted(name_map.items())
                  if len({h["sha256"] for h in holders}) > 1]
    view = {"document": "asset_library_view", "generated_at": now(),
            "profile": "FRAME_ANIMATION_V1", "facets": dict(FACETS),
            "rights_note": RIGHTS_UNVERIFIED["note"],
            "warnings": warnings, "name_collisions": collisions,
            "assets": assets,
            "summary": {
                "assets": len(assets),
                "revisions": sum(a["revisions_count"] for a in assets),
                "assigned": len(registry["assignments"]),
                "unprotected": sum(1 for a in assets for r in a["revisions"]
                                   if not r["protected"]),
                "corrupt": sum(1 for a in assets
                               if a["integrity"] == "CORRUPT"),
                "missing": sum(1 for a in assets
                               if a["integrity"] == "MISSING")}}
    return _filter_view(view, kind=kind, revision=revision, origin=origin,
                        rights=rights, shot_id=shot_id)


def _filter_view(view, *, kind, revision, origin, rights, shot_id):
    """Apply the browse filters to a computed view (asset level keeps only
    revisions that match; summary is recomputed)."""
    if rights is not None and rights != "UNVERIFIED":
        view["assets"] = []
    else:
        kept = []
        for asset in view["assets"]:
            revisions = [r for r in asset["revisions"]
                         if (kind is None or r["kind"] == kind)
                         and (revision is None or r["revision"] == revision)
                         and (origin is None or r["origin"] == origin)
                         and (shot_id is None or any(
                             u.get("shot_id") == shot_id
                             for u in r["used_in"]))]
            if revisions:
                kept.append(dict(asset, revisions=revisions,
                                 revisions_count=len(revisions)))
        view["assets"] = kept
    view["summary"].update(
        assets=len(view["assets"]),
        revisions=sum(a["revisions_count"] for a in view["assets"]),
        unprotected=sum(1 for a in view["assets"] for r in a["revisions"]
                        if not r["protected"]),
        corrupt=sum(1 for a in view["assets"]
                    for r in a["revisions"] if r["integrity"] == "CORRUPT"),
        missing=sum(1 for a in view["assets"]
                    for r in a["revisions"] if r["integrity"] == "MISSING"))
    return view


def detach_asset(project, shot_id):
    """Drop a cut's assignment pin from the current manifest.

    The cut then resolves no sequence: draft Preview falls back to its
    labelled placeholder and Final refuses the entry, while scope locks
    and reviews that bound the old pin go STALE by their own recomputation.
    The revision's registry record and bytes are untouched.
    """
    p = Path(project)
    with project_mutex(p):
        require_animation_profile(p)
        if not SHOT_ID.fullmatch(shot_id if type(shot_id) is str else ""):
            raise FilmError("shot_id must be an S001-style identifier")
        shots = read(p / "manifest/shots.json")
        if not any(s["id"] == shot_id for s in shots):
            raise FilmError(f"Unknown shot: {shot_id}")
        registry = load_registry(p)
        pin = registry["assignments"].get(shot_id)
        if pin is None:
            raise FilmError(f"{shot_id} has no assigned sequence to detach")
        del registry["assignments"][shot_id]
        save_registry(p, registry)
        staled = _locks_naming(p, shot_id)
        return {"detached": shot_id, "pin": pin, "state": "DRAFT",
                "unresolved": f"{shot_id} now resolves no sequence",
                "locks_staled": staled, "facets": dict(FACETS)}


def _locks_naming(p, shot_id):
    """Scope-lock records whose binding names this cut; they recompute
    STALE on their own after the pin is gone."""
    path = safe_path(p, LOCKS_PATH)
    if not path.is_file():
        return []
    try:
        document = read_canon(path)
    except FilmError:
        return []
    named = []
    for record in document.get("locks", []):
        cuts = (record.get("binding") or {}).get("cuts", [])
        if any(cut.get("shot_id") == shot_id for cut in cuts):
            named.append(f"{record['scope']} {record['lock_id']}"
                         + (f" {record['wave']}" if record.get("wave") else ""))
    return named


def replace_asset(project, asset_id, *, file=None, folder=None, index=None,
                  spec=None, shot_id=None, pivot=None, crop_origin=None,
                  z_order=None, note=""):
    """Open a new revision of an existing asset, linked to the manifest.

    Replacement is the ANIM-003 revision contract: changed content opens a
    new revision and the pinned bytes of previous revisions are never
    rewritten. Per kind the matching import path applies — sequences need
    the cut they stay assigned to, single-member kinds reuse the recorded
    context fields, and CONTROL_IMAGE keeps its packet/draft checks.
    COMPOSITE_SEQUENCE is produced by composite_shot, not imported, so it
    is refused here rather than silently switching kinds.
    """
    p = Path(project)
    require_animation_profile(p)
    registry = load_registry(p)
    entry = registry["assets"].get(asset_id)
    if entry is None:
        raise FilmError(f"Unknown asset: {asset_id}")
    record = entry["revisions"][str(entry["current_revision"])]
    kind = record["kind"]
    if kind == "COMPOSITE_SEQUENCE":
        raise FilmError("COMPOSITE_SEQUENCE revisions are produced by "
                        "composite_shot; replace the cut's compose, not "
                        "the stored sequence")
    given = [name for name, value in
             (("file", file), ("folder", folder), ("index", index),
              ("spec", spec)) if value is not None]
    if kind == "FRAME_SEQUENCE":
        if given != ["folder"] and given != ["index"]:
            raise FilmError("a FRAME_SEQUENCE replacement takes exactly one "
                            "of folder or index")
        if shot_id is None:
            raise FilmError("a FRAME_SEQUENCE replacement names the shot "
                            "it stays assigned to")
        return import_frame_sequence(p, shot_id, folder=folder, index=index,
                                     asset_id=asset_id, note=note)
    if kind == "RIG_SPEC":
        if given != ["spec"]:
            raise FilmError("a RIG_SPEC replacement takes the spec document")
        return import_rig_spec(p, spec, asset_id=asset_id, note=note)
    if given != ["file"]:
        raise FilmError(f"a {kind} replacement takes exactly one file")
    if kind == "LAYER_RGBA":
        return import_layer_rgba(
            p, file, asset_id=asset_id,
            pivot=pivot if pivot is not None else record["pivot"],
            crop_origin=(crop_origin if crop_origin is not None
                         else record["crop_origin"]),
            z_order=z_order if z_order is not None else record["z_order"],
            note=note)
    if kind == "MASK":
        return import_mask(p, file, target=record["target"],
                           region=record["region"],
                           channel=record["channel"], asset_id=asset_id,
                           note=note)
    if kind == "REPLACEMENT_DRAWING":
        return import_replacement_drawing(
            p, file, replaces=record["replaces"], frame=record["frame"],
            pivot=record["rig"]["pivot"], asset_id=asset_id, note=note)
    if kind == "CONTROL_IMAGE":
        return import_draft_image(
            p, record["shot_id"], file, role=record["control_role"],
            frame=record["frame"], references=record["references"],
            asset_id=asset_id, note=note)
    raise FilmError(f"Unsupported asset kind: {kind}")


def _scan_unused(p, registry):
    """Split every registered revision into deletable candidates and
    revisions the evidence chain still pins."""
    pin_refs, content_refs, _ = _collect_refs(p, registry, errors=None)
    candidates, protected = [], []
    for asset_id in sorted(registry["assets"]):
        entry = registry["assets"][asset_id]
        for label in sorted(entry["revisions"], key=int):
            record = entry["revisions"][label]
            refs = _refs_for(pin_refs, content_refs, asset_id, record)
            reasons = set(refs["reasons"])
            # The adopted current revision must always name a live record;
            # while other revisions remain it is never a delete candidate.
            if record["revision"] == entry["current_revision"] \
                    and len(entry["revisions"]) > 1:
                reasons.add(f"current revision of {asset_id}")
            row = {"asset_id": asset_id, "revision": record["revision"],
                   "kind": record["kind"],
                   "content_sha256": record["content_sha256"],
                   "members": len(record["files"]),
                   "bytes": sum(f["byte_length"] for f in record["files"]),
                   "reasons": sorted(reasons)}
            (protected if reasons else candidates).append(row)
    return candidates, protected


def _member_target(p, revision_dir, relative):
    """One recorded member's on-disk path, or a refusal reason.

    The path is joined lexically and never resolved: a member name that is
    absolute or carries a `..` component, a symlinked component anywhere
    between the project root and the file, or a normalised path that is
    not strictly inside the revision directory all refuse the revision's
    whole deletion plan.
    """
    if type(relative) is not str or not relative:
        return None, "member relative_name is empty or not a string"
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        return None, f"member path escapes the revision directory: {relative}"
    target = Path(os.path.normpath(str(Path(p) / relative)))
    if target == revision_dir or revision_dir not in target.parents:
        return None, f"member path is outside the revision directory: {relative}"
    node = Path(p)
    for part in Path(os.path.normpath(relative)).parts[:-1]:
        node = node / part
        if node.is_symlink():
            return None, f"member path crosses a symlink: {relative}"
    return target, None


def _revision_plan(p, asset_id, revision, record):
    """The full delete plan for one revision, or a refusal reason.

    Nothing is resolved and no symlink is followed: the revision directory
    and every component above it must be real directories, every recorded
    member must validate lexically inside it, and link objects found
    inside are unlinked as links — their targets are never touched.
    """
    node = Path(p)
    for part in (ASSET_ROOT, asset_id, f"r{revision}"):
        node = node / part
        if node.is_symlink():
            return None, (f"revision directory crosses a symlink: "
                          f"{ASSET_ROOT}/{asset_id}/r{revision}")
    revision_dir = node
    if revision_dir.exists() and not revision_dir.is_dir():
        return None, "revision path is not a directory"
    files, dirs = [], []
    for member in record["files"]:
        target, reason = _member_target(p, revision_dir,
                                        member["relative_name"])
        if reason is not None:
            return None, reason
        if (target.is_symlink() or target.is_file()) and target not in files:
            files.append(target)
    if revision_dir.is_dir():
        for root, dirnames, filenames in os.walk(revision_dir,
                                                 followlinks=False):
            root = Path(root)
            descend = []
            for name in dirnames:
                found = root / name
                if found.is_symlink():
                    if found not in files:
                        files.append(found)   # unlink the link, keep target
                elif found.is_dir():
                    dirs.append(found)
                    descend.append(name)
                else:
                    return None, \
                        f"non-regular entry in revision directory: {found}"
            dirnames[:] = descend
            for name in filenames:
                found = root / name
                if not found.is_symlink() and not found.is_file():
                    return None, \
                        f"non-regular file in revision directory: {found}"
                if found not in files:
                    files.append(found)
        dirs.append(revision_dir)
    return {"files": files, "dirs": dirs}, None


def cleanup_unused(project, *, execute=False):
    """Dry-run listing of unreferenced revisions; `execute` removes them.

    A revision is a deletion candidate only when the whole evidence chain
    is silent on it — never the current assignments, never a revision a
    current or historical build resolved or adopted, never one named by a
    scope LOCK, review, route decision, draft ledger, work packet or
    another asset's pin fields, and never the asset's adopted current
    revision while other revisions remain. On execute the evidence is
    recomputed inside the project mutex and only still-unprotected
    revisions lose their registry record and their own revision
    directory; nothing else on disk is touched.
    """
    p = Path(project)
    if not execute:
        require_animation_profile(p)
        registry = load_registry(p)
        candidates, protected = _scan_unused(p, registry)
        return {"dry_run": True, "candidates": candidates,
                "protected": protected, "deleted": [], "refused": [],
                "files_removed": 0, "facets": dict(FACETS),
                "note": "dry run; nothing was deleted"}
    with project_mutex(p):
        require_animation_profile(p)
        registry = load_registry(p)
        candidates, protected = _scan_unused(p, registry)
        # Validate every deletion plan before anything is removed: a
        # revision whose paths do not check out is refused whole and keeps
        # its registry record and its bytes.
        plans, refused = [], []
        for row in candidates:
            asset_id, revision = row["asset_id"], row["revision"]
            entry = registry["assets"].get(asset_id)
            record = entry["revisions"].get(str(revision)) \
                if entry else None
            reason = None
            if record is None:
                reason = "registry record is gone"
            elif revision == entry["current_revision"] \
                    and len(entry["revisions"]) > 1:
                reason = f"r{revision} is the current revision of {asset_id}"
            if reason is None:
                plan, reason = _revision_plan(p, asset_id, revision, record)
            if reason is not None:
                refused.append({**row, "status": "REFUSED",
                                "reason": reason})
            else:
                plans.append((row, plan))
        # The registry is updated first, so an error while unlinking can
        # only leave unrecorded files behind — never a record that claims
        # deleted bytes still exist.
        deleted = []
        for row, _plan in plans:
            entry = registry["assets"][row["asset_id"]]
            del entry["revisions"][str(row["revision"])]
            if entry["revisions"]:
                # current_revision moves only when the deleted revision was
                # the current one itself.
                if str(entry["current_revision"]) not in entry["revisions"]:
                    entry["current_revision"] = max(
                        int(r) for r in entry["revisions"])
            else:
                del registry["assets"][row["asset_id"]]
            deleted.append({**row, "status": "DELETED", "files_removed": 0})
        save_registry(p, registry)
        files_removed = 0
        for (row, plan), out in zip(plans, deleted):
            for target in plan["files"]:
                try:
                    os.unlink(target)      # unlinks links, never targets
                    out["files_removed"] += 1
                except OSError as exc:
                    out.setdefault("errors", []).append(str(exc))
            for folder in sorted(plan["dirs"], key=lambda d: -len(d.parts)):
                try:
                    folder.rmdir()
                except OSError as exc:
                    out.setdefault("errors", []).append(str(exc))
            asset_dir = Path(p) / ASSET_ROOT / row["asset_id"]
            try:
                if asset_dir.is_dir() and not asset_dir.is_symlink() \
                        and not any(asset_dir.iterdir()):
                    asset_dir.rmdir()
            except OSError as exc:
                out.setdefault("errors", []).append(str(exc))
            files_removed += out["files_removed"]
        return {"dry_run": False, "candidates": candidates,
                "protected": protected, "deleted": deleted,
                "refused": refused,
                "files_removed": files_removed, "facets": dict(FACETS),
                "note": "deleted only revisions no evidence chain pins"}
