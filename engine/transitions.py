"""FRAME_ANIMATION_V1 pairwise transitions and frame_map (ANIM-005).

Design 6.3-6.4 and schema section 3 fix the global frame layout: each entry
owns `[S_i, E_i)` output frames with `S_0 = 0`, `E_i = S_i + L_i` and
`S_(i+1) = E_i - O_i`, so `output_frames = sum(L_i) - sum(O_i)` — the doc
example `96 + 96 - 12 = 180` places the 12-frame transition at output
`[84, 96)`. A global frame has one or two contributing cut sources;
three-way coverage is refused by `validate_animation_timeline` and rechecked
here at the frame level.

Inside an O-frame overlap the `LINEAR_INTERIOR_V1` curve gives the 0-based
position `k` the incoming weight `(k + 1) / (O + 1)` and the outgoing
`1 - incoming`. Weights stay exact `Fraction`s and serialize as canonical
`[n, d]` pairs (O = 1 -> 1/2 each; the doc's output frame 89 -> S001 7/13,
S002 6/13). The first implementation is per-frame compositing; an FFmpeg
xfade path would have to prove the same frame count, weights and endpoints.

`plan_frame_map` binds the validated layout to the hash-pinned sequences and
ANIM-004 member maps, producing one schema section 9 row per output frame:
each source carries `instance_id`, `shot_id`, `local_frame_index` (the
resolved member index inside the pinned sequence — the source's local frame
index, not the output-local position), `sequence_revision` and the canonical
weight; `operations` records the typed transition recipe. The rows are the
Build 2 `frame_map.jsonl` format: `output_sha256` stays null until a compose
writes the PNG it hashes (ANIM-006 seals a completed build's map), and the
draft Preview keeps writing only its separate `draft_frame_map.jsonl`.

This module computes and verifies maps; it writes no approvals, no LOCK and
no build record, and a LEGACY_MV project is refused by the profile gate.
"""
from fractions import Fraction
from pathlib import Path
import re

from PIL import Image

from .animation_assets import resolve_shot_sequence
from .animation_schema import (SHOT_ID, canon_bytes, load_animation_timeline,
                               require_animation_profile,
                               validate_animation_timeline)
from .core import FilmError, atomic_text, digest
from .frame_clock import FRAME_FILE, frame_filename
from .frame_sequence import member_map_for_entry

LINEAR_INTERIOR = "LINEAR_INTERIOR_V1"
CROSSFADE = "CROSSFADE"

SOURCE_FIELDS = {"instance_id", "shot_id", "local_frame_index",
                 "sequence_revision", "weight"}
ROW_FIELDS = {"frame_index", "file", "output_sha256", "sources",
              "operations", "review_refs"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def pair_weights(k, overlap):
    """(outgoing, incoming) exact weights at 0-based overlap position `k`.

    LINEAR_INTERIOR_V1: incoming = (k + 1) / (O + 1), so both endpoints keep
    strictly positive weight — the frame before the overlap and the frame
    after it are single-source, never a 0-weight ghost contribution.
    """
    if type(k) is not int or type(overlap) is not int or overlap < 1:
        raise FilmError("Overlap position and length must be integers with overlap >= 1")
    if not 0 <= k < overlap:
        raise FilmError(f"Overlap position {k} is outside [0, {overlap})")
    incoming = Fraction(k + 1, overlap + 1)
    return 1 - incoming, incoming


def canon_weight(weight):
    """A Fraction as the schema's canonical `[n, d]` pair (gcd 1)."""
    if type(weight) is not Fraction or weight < 0:
        raise FilmError("Transition weights are non-negative exact rationals")
    return [weight.numerator, weight.denominator]


def frame_contributors(layout, frame_index):
    """([(entry_row, weight)], transition) covering one global output frame.

    `layout` is a `validate_animation_timeline`/`audit_timeline` result.
    Outside a transition exactly one entry covers the frame at weight 1; the
    checks are kept here too so a forged layout fails instead of silently
    emitting three-way coverage.
    """
    total = layout["output_frames"]
    if type(frame_index) is not int or not 0 <= frame_index < total:
        raise FilmError(f"frame_index must be inside [0, {total})")
    covering = [t for t in layout["transitions"]
                if t["output_range"][0] <= frame_index < t["output_range"][1]]
    if len(covering) > 1:
        raise FilmError("A global frame sits inside two transition ranges")
    if covering:
        transition = covering[0]
        if transition["type"] != CROSSFADE:
            raise FilmError("A non-crossfade transition cannot own output frames")
        outgoing, incoming = pair_weights(
            frame_index - transition["output_range"][0],
            transition["overlap_frames"])
        by_id = {r["instance_id"]: r for r in layout["entries"]}
        return ([(by_id[transition["from_instance"]], outgoing),
                 (by_id[transition["to_instance"]], incoming)], transition)
    rows = [r for r in layout["entries"]
            if r["output_range"][0] <= frame_index < r["output_range"][1]]
    if len(rows) != 1:
        raise FilmError("A global frame must be covered by exactly one entry "
                        "outside a transition")
    return [(rows[0], Fraction(1))], None


def coverage_counts(layout):
    """Per-frame contributor count over the whole output range."""
    counts = [0] * layout["output_frames"]
    for row in layout["entries"]:
        for frame in range(*row["output_range"]):
            counts[frame] += 1
    return counts


def audit_timeline(document, output_frames=None):
    """Length verification (길이 검산) for an `animation_timeline` document.

    Re-runs the full validator — total mismatch, out-of-range overlaps and
    three-cut coverage are rejections, never auto-repaired — then reports the
    used/overlap arithmetic and per-frame coverage back. The returned
    `entries` rows gain the entry's `used_source_range` and `used_frames` and
    double as the layout consumed by `frame_contributors`.
    """
    layout = validate_animation_timeline(document, output_frames)
    counts = coverage_counts(layout)
    used = 0
    for entry, row in zip(document["entries"], layout["entries"]):
        rng = entry["used_source_range"]
        row["used_source_range"] = list(rng)
        row["used_frames"] = rng[1] - rng[0]
        used += rng[1] - rng[0]
    return {"output_frames": layout["output_frames"],
            "used_source_frames": used,
            "overlap_frames": used - layout["output_frames"],
            "entries": layout["entries"],
            "transitions": layout["transitions"],
            "coverage": {
                "uncovered_frames": sum(1 for c in counts if c == 0),
                "single_source_frames": sum(1 for c in counts if c == 1),
                "shared_frames": sum(1 for c in counts if c == 2),
                "frames_with_three_or_more": sum(1 for c in counts if c >= 3)}}


def _map_rows(audit, resolved):
    """One schema section 9 row per global output frame."""
    rows = []
    for index in range(audit["output_frames"]):
        pairs, transition = frame_contributors(audit, index)
        sources = []
        for row, weight in pairs:
            info = resolved[row["instance_id"]]
            local = index - row["output_range"][0]
            member = info["member_map"][local]["member"]
            if member is None:
                raise FilmError(
                    f"{row['shot_id']} output frame {index} maps to a "
                    "state-only slot; a frame_map names real source frames")
            sources.append({"instance_id": row["instance_id"],
                            "shot_id": row["shot_id"],
                            "local_frame_index": member,
                            "sequence_revision": info["pin"]["revision"],
                            "weight": canon_weight(weight)})
        operations = []
        if transition is not None:
            operations.append({"type": CROSSFADE,
                               "transition_id": transition["id"],
                               "recipe": LINEAR_INTERIOR})
        rows.append({"frame_index": index,
                     "file": f"final_frames/{frame_filename(index)}",
                     "output_sha256": None,
                     "sources": sources,
                     "operations": operations,
                     "review_refs": []})
    return rows


def plan_frame_map(project, schedules=None):
    """The schema section 9 frame_map rows for a project's whole edit.

    Every timeline entry must resolve to its pinned sequence revision with
    hash-verified members — the map claims actual sources, so an unresolved
    shot is an error, not a placeholder. `schedules` may map an instance_id
    to that cut's drawing-track exposure schedules; the default is the
    identity (ones) map. The result is a draft plan: `output_sha256` stays
    null until a compose writes each PNG, and Build 2 sealing/inventory
    belong to ANIM-006. No review, LOCK or approval is created.
    """
    p = Path(project)
    config = require_animation_profile(p)
    document = load_animation_timeline(p)
    audit = audit_timeline(document, config["animation"]["output_frames"])
    entries = {e["instance_id"]: e for e in document["entries"]}
    resolved, report, unresolved = {}, [], []
    for row in audit["entries"]:
        entry = entries[row["instance_id"]]
        try:
            found = resolve_shot_sequence(p, entry["shot_id"],
                                          entry["used_source_range"],
                                          entry["unused_handles"],
                                          entry["sequence_revision"])
            resolved[row["instance_id"]] = {
                "member_map": member_map_for_entry(
                    entry, (schedules or {}).get(row["instance_id"])),
                "pin": {"asset_id": found["asset_id"],
                        "revision": found["revision"],
                        "content_sha256": found["content_sha256"]}}
            report.append({"instance_id": row["instance_id"],
                           "shot_id": entry["shot_id"],
                           "output_range": row["output_range"],
                           "resolved": True,
                           **resolved[row["instance_id"]]["pin"]})
        except FilmError as e:
            report.append({"instance_id": row["instance_id"],
                           "shot_id": entry["shot_id"],
                           "output_range": row["output_range"],
                           "resolved": False, "reason": str(e)})
            unresolved.append(f"{row['instance_id']} ({entry['shot_id']}): {e}")
    if unresolved:
        raise FilmError("frame_map needs every entry resolved to its pinned "
                        "sequence — " + "; ".join(unresolved))
    return {"state": "DRAFT", "output_frames": audit["output_frames"],
            "audit": {k: audit[k] for k in ("output_frames", "used_source_frames",
                                            "overlap_frames", "coverage",
                                            "transitions")},
            "entries": report, "rows": _map_rows(audit, resolved),
            "note": "Draft frame_map plan: output_sha256 stays null until a "
                    "compose writes each PNG; not a review, LOCK or sealed "
                    "Build 2 map"}


def _canon_pair(weight):
    """Validate one `[n, d]` weight and return it as a Fraction."""
    if (type(weight) is not list or len(weight) != 2
            or any(type(v) is not int for v in weight)):
        raise FilmError("weight must be an [n, d] integer pair")
    num, den = weight
    if num < 1 or den < 1:
        raise FilmError("A contributing source needs a positive weight")
    value = Fraction(num, den)
    if value.numerator != num or value.denominator != den:
        raise FilmError("weight is not in reduced canonical form")
    return value


def verify_frame_map(rows, document=None, *, output_frames=None,
                     complete=False):
    """Re-verify a frame_map against the timeline contract.

    Always checks one row per output frame in order, the `final_frames/`
    `F_######.png` names, exactly the schema section 9 fields, one or two
    sources with canonical weights summing to 1, and well-formed operations.
    With `document` (an `animation_timeline`) it additionally recomputes the
    contributors: instance order, weights, the CROSSFADE operation and each
    source's `local_frame_index` bound inside its used range. `complete=True`
    requires real `output_sha256` values (a composed map); a plan keeps them
    null. This is a recomputation check, not a review or approval.
    """
    if type(rows) is not list:
        raise FilmError("frame_map must be a list of rows")
    audit = audit_timeline(document) if document is not None else None
    expected = audit["output_frames"] if audit else output_frames
    if type(expected) is not int or expected < 1:
        raise FilmError("verify_frame_map needs a document or output_frames")
    if len(rows) != expected:
        raise FilmError(f"frame_map has {len(rows)} rows for {expected} frames")
    by_instance = ({e["instance_id"]: e for e in document["entries"]}
                   if document is not None else {})
    shared = 0
    for index, row in enumerate(rows):
        if type(row) is not dict or set(row.keys()) != ROW_FIELDS:
            raise FilmError(f"frame_map row {index} must hold exactly the "
                            "schema section 9 fields")
        if row["frame_index"] != index:
            raise FilmError(f"frame_map row order broken at row {index}")
        if row["file"] != f"final_frames/{frame_filename(index)}":
            raise FilmError(f"frame_map row {index} names the wrong output file")
        sha = row["output_sha256"]
        if sha is not None and not SHA256_RE.fullmatch(
                sha if type(sha) is str else ""):
            raise FilmError(f"frame_map row {index} has a malformed output_sha256")
        if complete and sha is None:
            raise FilmError(f"frame_map row {index} has no output hash")
        sources = row["sources"]
        if type(sources) is not list or not 1 <= len(sources) <= 2:
            raise FilmError(f"frame_map row {index} must have one or two sources")
        shared += len(sources) == 2
        total = Fraction(0)
        for source in sources:
            if type(source) is not dict or set(source.keys()) != SOURCE_FIELDS:
                raise FilmError(f"frame_map row {index} source must hold "
                                "exactly the schema fields")
            if type(source["instance_id"]) is not str \
                    or not source["instance_id"]:
                raise FilmError(f"frame_map row {index} has a bad instance_id")
            if not SHOT_ID.fullmatch(source["shot_id"]
                                     if type(source["shot_id"]) is str else ""):
                raise FilmError(f"frame_map row {index} has a bad shot_id")
            if type(source["local_frame_index"]) is not int \
                    or source["local_frame_index"] < 0:
                raise FilmError(f"frame_map row {index} has a bad "
                                "local_frame_index")
            if type(source["sequence_revision"]) is not int \
                    or source["sequence_revision"] < 1:
                raise FilmError(f"frame_map row {index} has a bad "
                                "sequence_revision")
            total += _canon_pair(source["weight"])
        if total != 1:
            raise FilmError(f"frame_map row {index} weights do not sum to 1")
        operations = row["operations"]
        if type(operations) is not list or any(
                type(o) is not dict or type(o.get("type")) is not str
                for o in operations):
            raise FilmError(f"frame_map row {index} has malformed operations")
        if type(row["review_refs"]) is not list or any(
                type(ref) is not str for ref in row["review_refs"]):
            raise FilmError(f"frame_map row {index} has malformed review_refs")
        if audit is None:
            continue
        pairs, transition = frame_contributors(audit, index)
        if len(pairs) != len(sources):
            raise FilmError(f"frame_map row {index} does not match the edit's "
                            "contributors")
        for (expected_row, weight), source in zip(pairs, sources):
            if source["instance_id"] != expected_row["instance_id"] \
                    or source["shot_id"] != expected_row["shot_id"] \
                    or Fraction(*source["weight"]) != weight:
                raise FilmError(f"frame_map row {index} does not match the "
                                "edit's contributors")
            used = by_instance[source["instance_id"]]["used_source_range"]
            if not used[0] <= source["local_frame_index"] < used[1]:
                raise FilmError(f"frame_map row {index} local_frame_index is "
                                "outside the entry's used range")
        fades = [o for o in operations if o.get("type") == CROSSFADE]
        if (transition is not None) != bool(fades):
            raise FilmError(f"frame_map row {index} CROSSFADE operation does "
                            "not match the transition coverage")
        if transition is not None:
            op = fades[0]
            if op.get("transition_id") != transition["id"] \
                    or op.get("recipe") != LINEAR_INTERIOR \
                    or len(fades) != 1:
                raise FilmError(f"frame_map row {index} carries the wrong "
                                "transition recipe")
    return {"verified": True, "frames": expected, "shared_frames": shared,
            "complete": complete}


def attach_frame_outputs(rows, frames_dir):
    """Fill each row's `output_sha256` from a real directory of F_*.png.

    The member read is the basename of the row's declared `file`, so a draft
    `draft_frames/` directory can stand in for the eventual `final_frames/`.
    A missing or symlinked member aborts the attach — a map never claims a
    hash for a frame that does not exist.
    """
    frames_dir = Path(frames_dir)
    attached = 0
    for row in rows:
        name = Path(row["file"]).name
        if not FRAME_FILE.fullmatch(name):
            raise FilmError(f"Not an F_000001-style frame name: {name}")
        path = frames_dir / name
        if path.is_symlink() or not path.is_file():
            raise FilmError(f"Missing composed frame for frame_map row: {name}")
        row["output_sha256"] = digest(path)
        attached += 1
    return attached


def frame_map_bytes(rows):
    """frame_map.jsonl bytes: one CANON_JSON_V1 document per line."""
    return b"".join(canon_bytes(row) for row in rows)


def write_frame_map(path, rows):
    """Atomic write of the canonical JSONL map."""
    atomic_text(path, frame_map_bytes(rows).decode("utf-8"))
    return Path(path)


def composite_pair(outgoing, incoming, incoming_weight):
    """Blend two same-canvas frames by the exact incoming weight.

    The rational weight is the contract; conversion to the float mix happens
    only at the pixel operation. Sources are fitted to the canvas before this
    call, so differing sizes are an error, never a silent resize.
    """
    if type(incoming_weight) is not Fraction or not 0 <= incoming_weight <= 1:
        raise FilmError("incoming_weight must be a rational in [0, 1]")
    if outgoing.size != incoming.size:
        raise FilmError("Transition sources must share one canvas size")
    if incoming_weight == 0:
        return outgoing.convert("RGB")
    if incoming_weight == 1:
        return incoming.convert("RGB")
    return Image.blend(outgoing.convert("RGB"), incoming.convert("RGB"),
                       float(incoming_weight))
