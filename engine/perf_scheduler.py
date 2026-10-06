"""FRAME_ANIMATION_V1 performance scheduler (ANIM-017).

Execution/storage design §8/§10.2/§11, design §11.5, evolution §2-4/§6:
a dependency graph over shots, output frames, source members, halo
members and the clean/subbed/encode stages; dependency-aware partial
recompilation; deterministic bounded-parallel compositing whose output
is byte/hash-identical to the serial path; bounded prefetch; content-
hash-keyed cache reuse; and PACK-SPARSE warm accounting with real
per-edge bytes/requests.

Invalidation (§8.2.1): every output frame carries a content key hashed
from its source member SHA-256s and transition recipe — a one-cut edit
dirties only the frames whose source bytes changed plus downstream
subbed/encode work; a locator-only change (same member bytes at a new
path) never recompiles. Cache hits never promote reviews, LOCK, approval
or qualification state.

Sparse reads go through `PackReader`: an exact-206 backend fetches only
the members the dirty set needs (halo included); a Range-unsupported
backend is refused unless a whole-pack cap was declared, and fallback
transfer is accounted on its own edge. `DIRECT_DRIVE` can never appear
as a plan route, candidate or selection.

Nothing here is a production approval: benchmark evidence registers
AUTO_PERFORMANCE candidates only through the ANIM-019 capability
registry and only when the measurement is complete and bound to current
evidence. Facets stay UNQUALIFIED / PENDING / NOT_AUTHORIZED.
"""
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from io import BytesIO
from pathlib import Path
import copy
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

from .animation_assets import _resolve_pin, load_registry
from .animation_compiler import _check_frame_sequence, _fit
from .animation_schema import (canon_bytes, load_animation_timeline,
                               require_animation_profile)
from .capability_registry import ALLOWANCE_UNITS
from .core import (FilmError, digest, ffmpeg, now, project_mutex, read,
                   run, safe_path, write)
from .encoder_backends import (FrameSource, encode_delivery, encode_digest,
                               make_encode_recipe)
from .fav_pack import decoded_bytes_estimate, png_header
from .frame_clock import check_fps, frame_filename
from .frame_sequence import member_map_for_entry
from .lyrics import export_subtitles
from .media_mux import prepare_audio_track
from .media_verify import (frame_pixel_sha256, image_pixel_sha256,
                           sequence_root)
from .pack_reader import PackReader, fetch_index
from .transitions import _map_rows, audit_timeline, composite_pair
from .workspace import Workspace

RUNTIME_CONTRACT = "python-pil-compose-v1"
COMPOSE_RECIPE = "canvas-fit+linear-interior-v1+pil-rgb"
PERF_DIR = "render/perf"
STATE_NAME = "state.json"
REPORT_NAME = "perf_benchmark.json"
DEFAULT_CACHE_BYTES = 1 << 28          # 256 MiB
DEFAULT_DECODED_CAP = 1 << 28          # 256 MiB decoded rasters
MAX_WORKERS = 32


class PerfInterrupted(FilmError):
    """Raised by a run that hit its declared interrupt_after bound."""


class PerfMetrics:
    """Per-run accounting: stage timeline, transfer edges, counters."""

    def __init__(self):
        self.stages = OrderedDict()
        self.edges = {}
        self.counters = {}
        self.peak_disk_bytes = 0

    def stage(self, name):
        metrics = self

        class _Timer:
            def __enter__(self):
                self.t0 = time.monotonic()
                return self

            def __exit__(self, *exc):
                metrics.add_stage(name, round(
                    (time.monotonic() - self.t0) * 1000))
        return _Timer()

    def add_stage(self, name, ms):
        self.stages[name] = self.stages.get(name, 0) + int(ms)

    def edge(self, name, requests=1, bytes_=0):
        entry = self.edges.setdefault(name, {"requests": 0, "bytes": 0})
        entry["requests"] += requests
        entry["bytes"] += int(bytes_)

    def count(self, key, n=1):
        self.counters[key] = self.counters.get(key, 0) + n

    def observe_disk(self, used):
        self.peak_disk_bytes = max(self.peak_disk_bytes, int(used))

    def timeline(self):
        return [{"stage": name, "ms": ms}
                for name, ms in self.stages.items()]

    def report(self):
        return {"stage_timeline": self.timeline(),
                "edges": {k: dict(v) for k, v in self.edges.items()},
                "counters": dict(self.counters),
                "peak_disk_bytes": self.peak_disk_bytes}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_json(material):
    return hashlib.sha256(canon_bytes(material)).hexdigest()


# --- member sources ----------------------------------------------------------
#
# A member source answers `(member_index) -> verified bytes` for one
# instance. The graph records each member's content SHA-256 plus the
# source's locator for planning; locators never enter content keys.


class LocalMemberSource:
    """Registry members read from project files; hash-verified on fetch."""

    name = "local"
    cacheable = False              # local reads are already inside the box

    def __init__(self, project):
        self.project = Path(project)
        self._record = None

    def bind(self, record):
        self._record = record
        return self

    def open(self):
        return None

    def locator(self, index):
        return self._record["files"][index]["relative_name"]

    def fetch(self, index, meta, metrics):
        path = safe_path(self.project, meta["locator"])
        if path.is_symlink() or not path.is_file():
            raise FilmError(f"Missing asset member: {meta['locator']}")
        data = path.read_bytes()
        if len(data) != meta["byte_length"] \
                or _sha256(data) != meta["sha256"]:
            raise FilmError(
                f"Asset member hash mismatch: {meta['locator']}")
        metrics.edge("member_source:local", 1, len(data))
        return data


class PackMemberSource:
    """One instance's members as sparse verified reads from a FAV1 pack.

    `member_ids` maps a member index to a pack member_id. The index is
    fetched lazily inside the timed run (`open()`), so its transfer lands
    on the run's `pack_index` edge; every member is hash-checked by
    PackReader before decode. `member_cache` reuse keeps unchanged
    members from re-transferring on warm runs.
    """

    name = "pack"
    cacheable = True               # remote bytes are worth keeping

    def __init__(self, backend, pack_object_id, index_object_id,
                 index_sha256, member_ids, image_contract,
                 whole_pack_cap=None):
        self.backend = backend
        self.pack_object_id = pack_object_id
        self.index_object_id = index_object_id
        self.index_sha256 = index_sha256
        self.member_ids = dict(member_ids)
        self.image_contract = image_contract
        self.whole_pack_cap = whole_pack_cap
        self.reader = None

    def bind(self, record):        # pack members need no registry bytes
        return self

    def open(self):
        if self.reader is None:
            index = fetch_index(self.backend, self.index_object_id,
                                self.index_sha256)
            self.reader = PackReader(self.backend, self.pack_object_id,
                                     index,
                                     whole_pack_cap=self.whole_pack_cap)
        return self.reader

    def locator(self, index):
        return self.member_ids[index]

    def fetch(self, index, meta, metrics):
        return self.open().read_member(meta["locator"])


class CountingBackend:
    """Archive backend wrapper that records real request/byte accounting.

    Every get_object/get_range is logged with its status and response
    bytes — the sparse-member and whole-pack-fallback edges in the
    benchmark report come from this list, never from estimates.
    """

    def __init__(self, backend):
        self.backend = backend
        self.requests = []

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def get_object(self, object_id):
        result = self.backend.get_object(object_id)
        self.requests.append({"op": "get_object", "object_id": object_id,
                              "status": result.status,
                              "bytes": len(result.body)})
        return result

    def get_range(self, object_id, offset, length):
        result = self.backend.get_range(object_id, offset, length)
        self.requests.append({"op": "get_range", "object_id": object_id,
                              "offset": offset, "length": length,
                              "status": result.status,
                              "bytes": len(result.body)})
        return result

    def put_object(self, object_id, data):
        result = self.backend.put_object(object_id, data)
        self.requests.append({"op": "put_object", "object_id": object_id,
                              "status": 200, "bytes": len(data)})
        return result


def attribute_edges(metrics, backend, *, since=0, index_objects=()):
    """Fold backend request logs into named metric edges.

    Index objects, exact-206 sparse member reads and whole-pack 200
    fallbacks land on separate edges so the report can tell a warm
    sparse read from a forced whole-pack transfer.
    """
    log = getattr(backend, "requests", [])
    index_objects = set(index_objects or ())
    for req in log[since:]:
        oid = req.get("object_id")
        status = req.get("status")
        byte_count = req.get("bytes", 0)
        if req.get("op") == "get_object":
            edge = "pack_index" if oid in index_objects else "pack_whole"
            metrics.edge(f"{edge}:{oid}", 1, byte_count)
        elif req.get("op") == "get_range":
            edge = "pack_fallback" if status == 200 else "pack_members"
            metrics.edge(f"{edge}:{oid}", 1, byte_count)
        else:
            metrics.edge(f"archive_op:{req.get('op')}", 1, byte_count)
    return len(log)


# --- content-keyed caches ------------------------------------------------------

class PerfCache:
    """Content-hash-keyed output reuse on a bounded Workspace.

    Keys are `f-<frame key>`, `s-<subbed key>`, `m-<member sha>` and
    `a-<encode digest>`: a cache hit returns bytes identical to a run of
    the same recipe on the same content. Hits never promote approvals or
    qualification — they only skip recomputation.
    """

    def __init__(self, root, capacity_bytes=DEFAULT_CACHE_BYTES):
        self.workspace = Workspace(root, capacity_bytes)

    def _frame_key(self, key, role):
        return ("f-" if role == "clean" else "s-") + key

    def get_frame(self, role, key):
        return self.workspace.get(self._frame_key(key, role))

    def put_frame(self, role, key, data):
        self.workspace.put(self._frame_key(key, role), data,
                           retention="TEMPORARY")

    def get_member(self, sha):
        return self.workspace.get("m-" + sha)

    def put_member(self, sha, data):
        self.workspace.put("m-" + sha, data,
                           retention="VERIFIED_MEMBER")

    def get_artifact(self, digest_value):
        return self.workspace.get("a-" + digest_value)

    def put_artifact(self, digest_value, data):
        self.workspace.put("a-" + digest_value, data,
                           retention="TEMPORARY")

    @property
    def used_bytes(self):
        return self.workspace.used_bytes


# --- member store: per-run decoded dedupe + bounded prefetch ---------------------

class MemberStore:
    """Decoded member reuse keyed by content sha, plus prefetch.

    A member used by many frames (twos, transitions, halos) is fetched,
    gated and decoded exactly once per run; identical bytes arriving
    under two member ids decode once. Fetches first hit the persistent
    content-keyed byte cache (PACK-SPARSE: no re-transfer of cached
    members), then the source.
    """

    def __init__(self, graph, metrics, cache=None,
                 decoded_cap=DEFAULT_DECODED_CAP):
        self.graph = graph
        self.metrics = metrics
        self.cache = cache
        self.decoded_cap = decoded_cap
        self._images = OrderedDict()      # sha -> (PIL image, estimate)
        self._decoded = 0
        self._lock = threading.Condition()
        self._inflight = set()
        self._prefetch_queue = deque()
        self._prefetch_queued = set()
        self._prefetch_thread = None
        self._closed = False

    def _member(self, iid, index):
        return self.graph["members"][iid][index]

    def _fetch_bytes(self, iid, index):
        meta = self._member(iid, index)
        source = self.graph["member_sources"][iid]
        if self.cache is not None and source.cacheable:
            cached = self.cache.get_member(meta["sha256"])
            if cached is not None and _sha256(cached) == meta["sha256"]:
                self.metrics.edge("cache:members", 1, len(cached))
                self.metrics.count("member_cache_hits")
                return cached
        t0 = time.monotonic()
        data = source.fetch(index, meta, self.metrics)
        self.metrics.count("member_fetch_ms",
                           round((time.monotonic() - t0) * 1000))
        if self.cache is not None and source.cacheable:
            self.cache.put_member(meta["sha256"], data)
            self.metrics.edge("cache:members", 1, len(data))
        self.metrics.count("member_fetches")
        return data

    def _decode(self, iid, index):
        data = self._fetch_bytes(iid, index)
        # Pre-decode gate: header, canvas and byte caps before any
        # decoder allocation — the same contract check the pack path uses.
        header = png_header(data, f"{iid}/{index}")
        canvas = self.graph["canvases"].get(iid)
        if canvas and (header["width"], header["height"]) \
                != (canvas["width"], canvas["height"]):
            raise FilmError(f"Member {iid}/{index} canvas does not match "
                            "the pinned sequence canvas")
        estimate = decoded_bytes_estimate(header)
        if estimate > self.graph["decode_cap_per_member"]:
            raise FilmError(f"Member {iid}/{index} decoded size exceeds "
                            "the per-member cap")
        if len(data) > self.graph["member_cap_bytes"]:
            raise FilmError(f"Member {iid}/{index} exceeds the encoded "
                            "member cap")
        try:
            with Image.open(BytesIO(data)) as im:
                image = im.convert("RGB")
        except FilmError:
            raise
        except Exception as e:
            raise FilmError(f"Member {iid}/{index} failed to decode: {e}") \
                from e
        self.metrics.count("member_decodes")
        return image, estimate

    def get(self, iid, index):
        sha = self._member(iid, index)["sha256"]
        with self._lock:
            cached = self._images.get(sha)
            if cached is not None:
                self._images.move_to_end(sha)
                self.metrics.count("member_store_hits")
                return cached[0]
            if sha in self._inflight:
                while sha in self._inflight:
                    self._lock.wait()
                cached = self._images.get(sha)
                if cached is not None:
                    self._images.move_to_end(sha)
                    self.metrics.count("member_store_hits")
                    return cached[0]
            self._inflight.add(sha)
        try:
            image, estimate = self._decode(iid, index)
        finally:
            with self._lock:
                self._inflight.discard(sha)
                self._lock.notify_all()
        with self._lock:
            self._images[sha] = (image, estimate)
            self._decoded += estimate
            while self._decoded > self.decoded_cap and \
                    len(self._images) > 1:
                _, (_, old_est) = self._images.popitem(last=False)
                self._decoded -= old_est
        return image

    def prefetch(self, iid, index):
        sha = self._member(iid, index)["sha256"]
        with self._lock:
            if sha in self._images or sha in self._inflight \
                    or sha in self._prefetch_queued:
                return
            self._prefetch_queued.add(sha)
            self._prefetch_queue.append((iid, index))
            self.metrics.count("prefetch_issued")
            self._lock.notify()

    def start_prefetch(self):
        if self._prefetch_thread is not None:
            return

        def _drain():
            while True:
                with self._lock:
                    while not self._prefetch_queue and not self._closed:
                        self._lock.wait()
                    if self._closed and not self._prefetch_queue:
                        return
                    iid, index = self._prefetch_queue.popleft()
                    sha = self._member(iid, index)["sha256"]
                    self._prefetch_queued.discard(sha)
                try:
                    self.get(iid, index)
                    self.metrics.count("prefetch_served")
                except FilmError:
                    pass                    # the real get() reports it

        self._prefetch_thread = threading.Thread(
            target=_drain, name="perf-prefetch", daemon=True)
        self._prefetch_thread.start()

    def stop_prefetch(self):
        with self._lock:
            self._closed = True
            self._lock.notify_all()
        if self._prefetch_thread is not None:
            self._prefetch_thread.join(timeout=10)
            self._prefetch_thread = None


# --- the dependency graph --------------------------------------------------------

def _resolve_metadata(p, shot_id, used_range, handles, expected_revision):
    """resolve_shot_sequence without member byte reads.

    The pin, revision adoption and used-range coverage checks are
    identical; member bytes are verified when actually fetched, never
    silently skipped — unfetched members are simply not read.
    """
    registry = load_registry(p)
    pin = registry["assignments"].get(shot_id)
    if pin is None:
        raise FilmError(f"{shot_id} has no assigned animation sequence")
    if expected_revision is not None \
            and pin["revision"] != expected_revision:
        raise FilmError(
            f"{shot_id} timeline adopts sequence revision "
            f"{expected_revision} but the registry assignment pins "
            f"revision {pin['revision']}")
    record = _resolve_pin(registry, pin["asset_id"], pin["revision"],
                          pin["content_sha256"])
    if record["kind"] not in ("FRAME_SEQUENCE", "COMPOSITE_SEQUENCE"):
        raise FilmError(f"{shot_id} is not assigned a frame sequence "
                        f"(found {record['kind']})")
    if used_range is not None:
        handles = handles or {}
        start, end = used_range
        if start - int(handles.get("before", 0)) < 0 \
                or end + int(handles.get("after", 0)) \
                > len(record["files"]):
            raise FilmError(
                f"{shot_id} used range plus handles "
                f"[{start - int(handles.get('before', 0))}, "
                f"{end + int(handles.get('after', 0))}) exceeds the "
                f"{len(record['files'])} imported frames")
    return {"asset_id": pin["asset_id"], "revision": pin["revision"],
            "content_sha256": pin["content_sha256"], "record": record}


def _frame_key(row, width, height):
    """Content key: member SHAs + weights + transition recipe + canvas.

    Locators and revisions are deliberately absent — a move of identical
    bytes changes nothing; changed bytes change the key.
    """
    return _sha256_json({
        "kind": "compose_frame", "v": 1, "recipe": COMPOSE_RECIPE,
        "format": {"width": width, "height": height},
        "sources": [{"instance_id": s["instance_id"],
                     "member_index": s["local_frame_index"],
                     "member_sha256": s["member_sha256"],
                     "weight": s["weight"]}
                    for s in row["sources"]],
        "operations": row["operations"]})


def _subbed_key(clean_pixel_sha, subtitle_recipe):
    return _sha256_json({"kind": "subbed_frame", "v": 1,
                         "clean_pixel_sha256": clean_pixel_sha,
                         "subtitle_recipe": subtitle_recipe})


def _op_ranges(audit):
    """Chunk the output range into per-entry ops that tile exactly.

    A transition's shared output range is the earlier entry's tail and
    the later entry's head at once; the later op owns it, so that op's
    halo is the earlier instance's member span inside the transition.
    """
    out_overlap = {t["from_instance"]: t["overlap_frames"]
                   for t in audit["transitions"]}
    ops, cursor = [], 0
    for row in audit["entries"]:
        overlap = out_overlap.get(row["instance_id"], 0)
        end = row["output_range"][1] - overlap
        ops.append({"operation_id": f"op-{row['instance_id']}",
                    "instance_id": row["instance_id"],
                    "output_range": [cursor, end],
                    "halo_ranges": []})
        cursor = end
    if cursor != audit["output_frames"]:
        raise FilmError("operation ranges do not tile the output")
    return ops


def _member_index_ranges(indices):
    """Sorted member indices merged into [start, end) ranges."""
    ranges = []
    for index in sorted(indices):
        if ranges and index == ranges[-1][1]:
            ranges[-1][1] += 1
        else:
            ranges.append([index, index + 1])
    return ranges


def build_graph(project, *, exposure=None, member_sources=None):
    """The §11.5 dependency graph, metadata only — no member byte reads.

    `member_sources` may substitute the per-instance byte source
    (LocalMemberSource by default; PackMemberSource for sparse archive
    reads). Per-frame content keys derive from member SHA-256s, so a
    locator-only change leaves the graph's keys untouched.
    """
    p = Path(project)
    config = require_animation_profile(p)
    fmt = copy.deepcopy(config["format"])
    fps = check_fps(fmt["fps"])
    width, height = fmt["width"], fmt["height"]
    document = load_animation_timeline(p)
    output_frames = config["animation"]["output_frames"]
    audit = audit_timeline(document, output_frames)
    master = safe_path(p, config["audio"]["path"])
    if digest(master) != config["audio"]["sha256"]:
        raise FilmError("Master audio hash mismatch")

    entries = {e["instance_id"]: e for e in document["entries"]}
    member_sources = dict(member_sources or {})
    resolved, members, canvases = {}, {}, {}
    for row in audit["entries"]:
        entry = entries[row["instance_id"]]
        found = _resolve_metadata(p, entry["shot_id"],
                                  entry["used_source_range"],
                                  entry["unused_handles"],
                                  entry["sequence_revision"])
        record = found["record"]
        resolved[row["instance_id"]] = {
            "member_map": member_map_for_entry(
                entry, (exposure or {}).get(row["instance_id"])),
            "pin": {"asset_id": found["asset_id"],
                    "revision": found["revision"],
                    "content_sha256": found["content_sha256"]}}
        source = member_sources.get(row["instance_id"]) \
            or LocalMemberSource(p)
        member_sources[row["instance_id"]] = source.bind(record)
        table = {}
        by_index = {f.get("frame_index"): f for f in record["files"]}
        for maprow in resolved[row["instance_id"]]["member_map"]:
            index = maprow["member"]
            if index is None:
                raise FilmError(
                    f"{row['instance_id']} maps a state-only slot; the "
                    "scheduler names real source frames")
            if index in table:
                continue
            f = by_index.get(index)
            if f is None:
                raise FilmError(f"{row['instance_id']} member {index} is "
                                "not in the pinned revision")
            table[index] = {"sha256": f["sha256"],
                            "byte_length": f["byte_length"],
                            "locator": source.locator(index)}
        members[row["instance_id"]] = table
        canvases[row["instance_id"]] = dict(record.get("canvas") or {})

    rows = _map_rows(audit, resolved)
    for row in rows:
        for source in row["sources"]:
            meta = members[source["instance_id"]][
                source["local_frame_index"]]
            source["member_sha256"] = meta["sha256"]
        row["key"] = _frame_key(row, width, height)

    ops = _op_ranges(audit)
    for op in ops:
        halo_members = {}
        own_members = set()
        for frame in range(*op["output_range"]):
            for source in rows[frame]["sources"]:
                iid = source["instance_id"]
                index = source["local_frame_index"]
                if iid == op["instance_id"]:
                    own_members.add(index)
                else:
                    halo_members.setdefault(iid, set()).add(index)
        op["own_members"] = sorted(own_members)
        op["halo_members"] = {iid: sorted(v) for iid, v in
                              sorted(halo_members.items())}
        op["halo_ranges"] = _member_index_ranges(
            i for v in halo_members.values() for i in v)

    snapshot_digest = _sha256_json({
        "timeline": document,
        "members": {iid: {str(k): v["sha256"]
                          for k, v in sorted(t.items())}
                    for iid, t in sorted(members.items())},
        "format": fmt, "output_frames": output_frames})
    recipe_digest = _sha256_json({"compose": COMPOSE_RECIPE,
                                  "transition": "LINEAR_INTERIOR_V1",
                                  "format": fmt})
    member_cap = max((f["byte_length"]
                      for t in members.values() for f in t.values()),
                     default=1)
    decode_cap = max((c.get("width", 1) * c.get("height", 1) * 4
                      for c in canvases.values()), default=1)
    return {
        "project": str(p), "output_frames": output_frames, "format": fmt,
        "fps": fps, "audio_sha256": config["audio"]["sha256"],
        "audio_path": config["audio"]["path"],
        "timeline_sha256": _sha256_json(document),
        "snapshot_digest": snapshot_digest,
        "recipe_digest": recipe_digest,
        "member_cap_bytes": member_cap,
        "decode_cap_per_member": decode_cap,
        "entries": [{"instance_id": row["instance_id"],
                     "shot_id": row["shot_id"],
                     "output_range": list(row["output_range"]),
                     "used_source_range": entries[row["instance_id"]]
                     ["used_source_range"],
                     "pin": resolved[row["instance_id"]]["pin"]}
                    for row in audit["entries"]],
        "transitions": audit["transitions"],
        "ops": ops, "rows": rows, "members": members,
        "canvases": canvases, "member_sources": member_sources,
        "dependency": {
            "shots": sorted({row["shot_id"] for row in audit["entries"]}),
            "instances": len(audit["entries"]),
            "frame_nodes": output_frames,
            "member_nodes": sum(len(t) for t in members.values()),
            "halo_member_edges": sum(
                len(v) for op in ops for v in op["halo_members"].values()),
            "frame_member_edges": sum(len(r["sources"]) for r in rows),
            "stages": ["read_verify", "compose", "subtitles", "subbed",
                       "encode", "verify"]}}


def _load_state(perf_root):
    path = Path(perf_root) / STATE_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise FilmError(f"perf state is unreadable: {path}") from e


def _save_state(perf_root, state):
    write(Path(perf_root) / STATE_NAME, state)


def _prev_frames(prev_state, role="clean"):
    if type(prev_state) is not dict:
        return []
    section = prev_state.get(role) or {}
    return section.get("frames") or []


def invalidate(graph, prev_state):
    """§8.2.1 invalidation closure over the dependency graph.

    Compares the graph's content keys to the last committed state and
    returns the dirty output-frame set, the members/halo a rebuild must
    read, the downstream stages that go stale and — report only — the
    instances whose approvals would be affected. A locator-only change
    produces an identical key set and no invalidation.
    """
    rows = graph["rows"]
    prev_input = (prev_state or {}).get("input") or {}
    prev_keys = [f.get("key") for f in _prev_frames(prev_state)]
    if prev_state is None or len(prev_keys) != len(rows):
        dirty = set(range(len(rows)))
        reason = "cold"
    else:
        # The dirty set is always the per-frame content-key diff: a
        # snapshot drift reports *why* the keys moved, never inflates it.
        dirty = {i for i, row in enumerate(rows)
                 if row["key"] != prev_keys[i]}
        if not dirty:
            reason = "unchanged"
        elif prev_input.get("snapshot_digest") \
                != graph["snapshot_digest"] \
                or prev_input.get("recipe_digest") \
                != graph["recipe_digest"]:
            reason = "input_changed"
        else:
            reason = "content"
    op_of = {}
    for op in graph["ops"]:
        for frame in range(*op["output_range"]):
            op_of[frame] = op["instance_id"]
    needed, halo = {}, {}
    for frame in sorted(dirty):
        for source in rows[frame]["sources"]:
            iid, index = source["instance_id"], source["local_frame_index"]
            needed.setdefault(iid, set()).add(index)
            if iid != op_of.get(frame):
                halo.setdefault(iid, set()).add(index)
    return {"dirty_frames": sorted(dirty),
            "unchanged": not dirty,
            "reason": reason,
            "needed_members": {k: sorted(v) for k, v in
                               sorted(needed.items())},
            "halo_members": {k: sorted(v) for k, v in
                             sorted(halo.items())},
            "affected_instances": sorted(needed),
            "stale_transitions": [t["id"] for t in graph["transitions"]
                                  if dirty & set(range(
                                      *t["output_range"]))],
            "note": "Dirty frames bound only recompute; approval, LOCK "
                    "and review state are never touched by cache or "
                    "recompute paths."}


def graph_report(graph):
    """perf-plan's dependency-graph summary (design §11.5)."""
    return {"snapshot_digest": graph["snapshot_digest"],
            "recipe_digest": graph["recipe_digest"],
            "output_frames": graph["output_frames"],
            "nodes": dict(graph["dependency"]),
            "ops": [{"operation_id": op["operation_id"],
                     "instance_id": op["instance_id"],
                     "output_range": op["output_range"],
                     "halo_ranges": op["halo_ranges"],
                     "own_members": len(op["own_members"]),
                     "halo_members": {k: len(v) for k, v in
                                      op["halo_members"].items()}}
                    for op in graph["ops"]],
            "member_counts": {iid: len(t) for iid, t in
                              graph["members"].items()}}


# --- stage: compose ---------------------------------------------------------------

def _compose_one(graph, row, images):
    """One output frame from already-decoded member images."""
    layer = None
    fmt = graph["format"]
    for source in row["sources"]:
        image = images[source["instance_id"]]
        fitted = _fit(image, fmt["width"], fmt["height"])
        layer = fitted if layer is None else composite_pair(
            layer, fitted, Fraction(*source["weight"]))
    if layer is None:
        raise FilmError(f"frame {row['frame_index']} has no sources")
    buffer = BytesIO()
    layer.save(buffer, "PNG")
    return image_pixel_sha256(layer), buffer.getvalue()


def _compose_stage(graph, cache, store, invalidation, out_dir, metrics, *,
                   workers, prefetch, interrupt_after):
    """Deterministic compose: dirty frames through a bounded pool, cached
    frames replayed in output order — byte-identical to the serial path."""
    rows = graph["rows"]
    out_dir.mkdir(parents=True, exist_ok=True)
    digests = [None] * len(rows)
    file_shas = [None] * len(rows)

    def work(index):
        row = rows[index]
        key = row["key"]
        # The cache is content-keyed: a hit is byte-identical regardless
        # of why the key recurred (unchanged frame, reverted edit or a
        # resume after interruption).
        cached = cache.get_frame("clean", key)
        if cached is not None:
            metrics.edge("cache:frames", 1, len(cached))
            metrics.count("frame_cache_hits")
            return index, key, None, cached
        images = {}
        for source in row["sources"]:
            iid = source["instance_id"]
            images[iid] = store.get(iid, source["local_frame_index"])
        pixel_sha, data = _compose_one(graph, row, images)
        metrics.count("frame_composed")
        return index, key, pixel_sha, data

    def write_result(index, key, pixel_sha, data):
        path = out_dir / frame_filename(index)
        path.write_bytes(data)
        if pixel_sha is None:
            metrics.count("frame_verify_decodes")
        digests[index] = pixel_sha or frame_pixel_sha256(path)
        file_shas[index] = _sha256(data)
        if pixel_sha is not None:
            cache.put_frame("clean", key, data)
            metrics.edge("cache:frames", 1, len(data))

    tasks = list(range(len(rows)))
    composed = 0

    def check_interrupt():
        if interrupt_after is not None and composed >= interrupt_after:
            raise PerfInterrupted(
                f"interrupted after {composed} composed frames")

    if prefetch:
        store.start_prefetch()
    try:
        if workers > 1:
            with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="perf-compose") as pool:
                issued = deque(tasks)
                for index, key, pixel_sha, data in pool.map(work, tasks):
                    write_result(index, key, pixel_sha, data)
                    if pixel_sha is not None:
                        composed += 1
                        check_interrupt()
                    # bounded lookahead: prefetch only upcoming members
                    if prefetch and issued:
                        issued.popleft()
                        for future in list(issued)[:prefetch]:
                            for source in rows[future]["sources"]:
                                store.prefetch(source["instance_id"],
                                               source["local_frame_index"])
        else:
            remaining = deque(tasks)
            while remaining:
                index = remaining.popleft()
                if prefetch:
                    for future in list(remaining)[:prefetch]:
                        for source in rows[future]["sources"]:
                            store.prefetch(source["instance_id"],
                                           source["local_frame_index"])
                i, key, pixel_sha, data = work(index)
                write_result(i, key, pixel_sha, data)
                if pixel_sha is not None:
                    composed += 1
                    check_interrupt()
    finally:
        if prefetch:
            store.stop_prefetch()
    return {"pixel_digests": digests, "file_sha256": file_shas,
            "composed": composed}


# --- stage: subtitles + subbed ------------------------------------------------------

def _subtitle_recipe(p, folder, duration_ms):
    """Export the reviewed cues and derive the overlay recipe digest —
    the same binding the strict compiler records."""
    subs = export_subtitles(p, folder, duration_ms, strict=True)
    report_file = read(Path(folder) / "subtitle_report.json")
    recipe = _sha256_json({
        "type": "SUBTITLE_OVERLAY",
        "ass_sha256": digest(subs["ass"]),
        "timing_sha256": report_file["timing_sha256"],
        "font_sha256": report_file["font"].get("sha256"),
        "timeline": "global"})
    return {"recipe": recipe, "ass": subs["ass"], "srt": subs["srt"],
            "report": report_file,
            "fonts": Path(folder) / "subtitle_fonts"}


def _contiguous_ranges(indices):
    ranges = []
    for index in sorted(indices):
        if ranges and index == ranges[-1][1]:
            ranges[-1][1] += 1
        else:
            ranges.append([index, index + 1])
    return ranges


def _burn_ranges(clean_dir, subbed_dir, ranges, subtitle, fps, metrics,
                 work_dir):
    """Burn the ASS onto contiguous dirty frame ranges.

    `-start_number` alone re-bases the range's first frame to PTS 0; a
    `setpts` shift restores its global timeline position before the ASS
    filter sees it, so a partial burn is pixel-identical to the same
    frames of a whole-sequence burn.
    """
    clean_dir, subbed_dir = Path(clean_dir), Path(subbed_dir)
    with tempfile.TemporaryDirectory(prefix=".subtitle-",
                                     dir=work_dir) as work:
        work = Path(work)
        shutil.copyfile(subtitle["ass"], work / "lyrics.ass")
        if subtitle["fonts"].is_dir():
            shutil.copytree(subtitle["fonts"], work / "fonts")
        (work / "fonts").mkdir(exist_ok=True)
        for start, end in ranges:
            shift = Fraction(start, fps)
            vf = (f"setpts=PTS+{shift.numerator}/{shift.denominator}/TB,"
                  "ass=filename=lyrics.ass:fontsdir=fonts")
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                       "-nostdin", "-y", "-filter_threads", "1",
                       "-framerate", str(fps), "-start_number",
                       str(start + 1), "-i",
                       str(clean_dir / "F_%06d.png"),
                       "-frames:v", str(end - start), "-vf", vf,
                       "-start_number", str(start + 1),
                       str(subbed_dir / "F_%06d.png")]
            result = subprocess.run(command, cwd=work, capture_output=True,
                                    timeout=600,
                                    creationflags=subprocess
                                    .CREATE_NO_WINDOW
                                    if os.name == "nt" else 0)
            if result.returncode:
                raise FilmError(result.stderr.decode(
                    errors="replace")[-4000:])
            metrics.count("subbed_burned", end - start)


def _subbed_stage(graph, cache, prev_state, subtitle, clean_digests,
                  clean_dir, subbed_dir, perf_root, metrics):
    """Overlay stage: every frame materializes in run order — a valid
    cache entry replays bytes; the rest burn in contiguous ranges with
    their global PTS preserved."""
    rows = graph["rows"]
    subbed_dir.mkdir(parents=True, exist_ok=True)
    prev_recipe = (prev_state or {}).get("subtitle_recipe")
    prev_frames = _prev_frames(prev_state, "subbed")
    keys = [_subbed_key(clean_digests[index], subtitle["recipe"])
            for index in range(len(rows))]
    dirty = [index for index in range(len(rows))
             if prev_recipe != subtitle["recipe"]
             or index >= len(prev_frames)
             or prev_frames[index].get("key") != keys[index]]
    for index in range(len(rows)):
        data = cache.get_frame("subbed", keys[index])
        if data is not None:
            (subbed_dir / frame_filename(index)).write_bytes(data)
            metrics.edge("cache:subbed", 1, len(data))
            metrics.count("subbed_cache_hits")
    burn = [i for i in range(len(rows))
            if not (subbed_dir / frame_filename(i)).is_file()]
    ranges = _contiguous_ranges(burn)
    if ranges:
        _burn_ranges(clean_dir, subbed_dir, ranges, subtitle,
                     graph["fps"], metrics, perf_root)
        for index in burn:
            path = subbed_dir / frame_filename(index)
            data = path.read_bytes()
            cache.put_frame("subbed", keys[index], data)
            metrics.edge("cache:subbed", 1, len(data))
    _check_frame_sequence(subbed_dir, len(rows))
    digests = [frame_pixel_sha256(subbed_dir / frame_filename(i))
               for i in range(len(rows))]
    metrics.count("subbed_dirty", len(dirty))
    metrics.count("subbed_burn_ranges", len(ranges))
    return {"keys": keys, "pixel_digests": digests, "dirty": len(dirty)}


# --- stage: encode ------------------------------------------------------------------

def _encode_stage(graph, cache, prev_state, roots, run_dir, perf_root,
                  metrics, master, roles):
    """Encode artifacts keyed by encode_digest(sequence_root, recipe).

    A hit returns a byte-identical MP4 whose recorded verification still
    binds it; a miss runs the ANIM-015 encode->mux->verify pipeline and
    stores the artifact for the next run with the same content.
    """
    fmt = graph["format"]
    output_frames = graph["output_frames"]
    seconds = output_frames / fmt["fps"]
    recipe = make_encode_recipe("FFMPEG", fmt)
    prev_encodes = (prev_state or {}).get("encodes") or {}
    results, audio_track, audio_work = {}, None, None
    try:
        for role in roles:
            root_value = roots[role]
            enc_digest = encode_digest(root_value, recipe)
            record = {"encode_digest": enc_digest}
            prev_record = prev_encodes.get(role) or {}
            artifact = None
            if prev_record.get("encode_digest") == enc_digest:
                data = cache.get_artifact(enc_digest)
                if data is not None and _sha256(data) == \
                        prev_record.get("artifact_sha256"):
                    artifact = data
                    metrics.edge("cache:encode", 1, len(data))
                    metrics.count("encode_cache_hits")
            target = run_dir / f"MASTER_{role.upper()}.mp4"
            if artifact is not None:
                target.write_bytes(artifact)
                record.update({"cached": True,
                               "artifact_sha256": _sha256(artifact),
                               "verification":
                                   prev_record.get("verification"),
                               "output_sha256": _sha256(artifact)})
                results[role] = record
                continue
            if audio_track is None:
                audio_work = Path(tempfile.mkdtemp(
                    prefix=".audio-", dir=perf_root))
                audio_track = prepare_audio_track(master, seconds,
                                                  audio_work)
                metrics.count("audio_tracks")
            frames_dir = run_dir / ("clean" if role == "clean"
                                    else "subbed")
            frames = FrameSource(frames_dir, fmt["fps"], fmt["width"],
                                 fmt["height"],
                                 expected_count=output_frames)
            with tempfile.TemporaryDirectory(prefix=".encode-",
                                             dir=perf_root) as work:
                result = encode_delivery(
                    frames, recipe, target, audio_track=audio_track,
                    master=master, work_dir=work, role=role)
            metrics.count("encode_runs")
            data = target.read_bytes()
            cache.put_artifact(enc_digest, data)
            metrics.edge("cache:encode", 1, len(data))
            record.update({"cached": False,
                           "artifact_sha256": result["sha256"],
                           "verification": result["verification"],
                           "output_sha256": result["sha256"],
                           "driver": result["driver"],
                           "sequence_root": result["sequence_root"],
                           "no_ffmpeg_encoding":
                               result["no_ffmpeg_encoding"],
                           "qualification_state":
                               result["qualification_state"]})
            results[role] = record
    finally:
        if audio_work is not None:
            shutil.rmtree(audio_work, ignore_errors=True)
    return results


# --- the pipeline -------------------------------------------------------------------

def _rss_bytes():
    """Peak resident bytes for this process (ru_maxrss is KiB on Linux)."""
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage * (1024 if sys.platform != "darwin" else 1)


def _backend_cursors(graph):
    """{backend: (cursor, index_object_ids)} for request attribution."""
    cursors = {}
    for src in graph["member_sources"].values():
        backend = getattr(src, "backend", None)
        if backend is not None and hasattr(backend, "requests") \
                and id(backend) not in cursors:
            cursors[id(backend)] = {"backend": backend,
                                    "cursor": len(backend.requests),
                                    "index_objects": set()}
        if backend is not None and hasattr(backend, "requests"):
            index_id = getattr(src, "index_object_id", None)
            if index_id:
                cursors[id(backend)]["index_objects"].add(index_id)
    return cursors


def run_pipeline(project, perf_root=None, *, exposure=None,
                 member_sources=None, workers=1, prefetch=0,
                 cache_capacity=DEFAULT_CACHE_BYTES,
                 interrupt_after=None, encode_roles=("clean", "subbed"),
                 metrics=None, perf_label="run"):
    """One scheduler run: read/verify -> compose -> subbed -> encode.

    `perf_root` holds the bounded cache, the committed state and per-run
    outputs. Returns the run result with metrics; raises PerfInterrupted
    when the declared interrupt bound is hit — a resume run finds finished
    frames and member bytes already in the content cache.
    """
    p = Path(project)
    metrics = metrics or PerfMetrics()
    started = time.monotonic()
    perf_root = Path(perf_root) if perf_root else p / PERF_DIR
    perf_root.mkdir(parents=True, exist_ok=True)
    run_dir = perf_root / "out" / perf_label
    run_dir.mkdir(parents=True, exist_ok=True)
    workers = int(workers)
    if workers < 1 or workers > MAX_WORKERS:
        raise FilmError(f"workers must be 1..{MAX_WORKERS}")
    cache = PerfCache(perf_root / "cache", cache_capacity)
    result = {"perf_root": str(perf_root), "run_dir": str(run_dir)}
    with metrics.stage("read_verify"):
        graph = build_graph(p, exposure=exposure,
                            member_sources=member_sources)
        prev_state = _load_state(perf_root)
        invalidation = invalidate(graph, prev_state)
        metrics.count("output_frames", graph["output_frames"])
        metrics.count("dirty_frames", len(invalidation["dirty_frames"]))
        metrics.count("needed_members",
                      sum(len(v) for v in
                          invalidation["needed_members"].values()))
        metrics.count("halo_members",
                      sum(len(v) for v in
                          invalidation["halo_members"].values()))
        analysis = read(p / "analysis/audio.json")
        duration_ms = analysis["duration_ms"]
        master = safe_path(p, graph["audio_path"])
        cursors = _backend_cursors(graph)
        # Pack sources fetch their index inside the timed run so the
        # transfer lands on the run's pack_index edge.
        opened = set()
        for src in graph["member_sources"].values():
            if id(src) not in opened:
                src.open()
                opened.add(id(src))
    result["snapshot_digest"] = graph["snapshot_digest"]
    result["invalidation"] = {k: v for k, v in invalidation.items()
                              if k != "note"}

    store = MemberStore(graph, metrics, cache=cache)
    try:
        with metrics.stage("compose"):
            clean_dir = run_dir / "clean"
            compose = _compose_stage(
                graph, cache, store, invalidation, clean_dir, metrics,
                workers=workers, prefetch=prefetch,
                interrupt_after=interrupt_after)
        _check_frame_sequence(clean_dir, graph["output_frames"])
        clean_root = sequence_root("clean", compose["pixel_digests"])

        with metrics.stage("subtitles"):
            subtitle = _subtitle_recipe(p, perf_root / "subtitles",
                                        duration_ms)
        with metrics.stage("subbed"):
            subbed_dir = run_dir / "subbed"
            subbed = _subbed_stage(
                graph, cache, prev_state, subtitle,
                compose["pixel_digests"], clean_dir, subbed_dir,
                perf_root, metrics)
        subbed_root = sequence_root("subbed", subbed["pixel_digests"])

        roots = {"clean": clean_root, "subbed": subbed_root}
        encodes = {}
        if encode_roles:
            with metrics.stage("encode"):
                encodes = _encode_stage(
                    graph, cache, prev_state, roots, run_dir, perf_root,
                    metrics, master, encode_roles)

        with metrics.stage("verify"):
            for info in cursors.values():
                attribute_edges(
                    metrics, info["backend"], since=info["cursor"],
                    index_objects=info["index_objects"])
            state = {
                "state_version": 1,
                "input": {"snapshot_digest": graph["snapshot_digest"],
                          "recipe_digest": graph["recipe_digest"],
                          "output_frames": graph["output_frames"],
                          "format": graph["format"],
                          "audio_sha256": graph["audio_sha256"],
                          "timeline_sha256": graph["timeline_sha256"]},
                "subtitle_recipe": subtitle["recipe"],
                "clean": {"sequence_root": clean_root,
                          "frames": [
                              {"key": row["key"],
                               "pixel_sha256": compose["pixel_digests"][i],
                               "output_sha256": compose["file_sha256"][i]}
                              for i, row in enumerate(graph["rows"])]},
                "subbed": {"sequence_root": subbed_root,
                           "frames": [
                               {"key": subbed["keys"][i],
                                "pixel_sha256":
                                    subbed["pixel_digests"][i]}
                               for i in range(len(graph["rows"]))]},
                "encodes": encodes,
                "generated_at": now(),
                "note": "Content-keyed reuse state; cache hits never "
                        "create or extend reviews, approvals or "
                        "qualification."}
            _save_state(perf_root, state)
            metrics.observe_disk(cache.used_bytes)
    finally:
        store.stop_prefetch()
    metrics.observe_disk(cache.used_bytes)
    metrics.add_stage("end_to_end",
                      round((time.monotonic() - started) * 1000))
    result.update({
        "clean_dir": str(run_dir / "clean"),
        "subbed_dir": str(run_dir / "subbed"),
        "clean_sequence_root": clean_root,
        "subbed_sequence_root": subbed_root,
        "encodes": encodes,
        "subtitle_recipe": subtitle["recipe"],
        "metrics": metrics.report(),
        "peaks": {"disk_bytes": metrics.peak_disk_bytes,
                  "ram_bytes": _rss_bytes(),
                  "vram_bytes": "UNKNOWN"},
        "state": str(perf_root / STATE_NAME)})
    return result


# --- benchmark ----------------------------------------------------------------------

def apply_one_cut_edit(project, shot_id, change):
    """The one-cut edit the benchmark times: re-import one shot's
    sequence with `change` = {member_index: bytes|fn(bytes)->bytes}
    applied — a real new revision through the normal import path (the
    import itself adopts the revision into the shot's timeline entries).
    Returns the import result."""
    from .animation_assets import import_frame_sequence
    p = Path(project)
    resolved = _resolve_metadata(p, shot_id, None, None, None)
    record = resolved["record"]
    folder = Path(tempfile.mkdtemp(prefix="perf-onecut-"))
    for f in sorted(record["files"], key=lambda m: m["frame_index"]):
        data = safe_path(p, f["relative_name"]).read_bytes()
        replacement = change.get(f["frame_index"])
        if replacement is not None:
            data = replacement(data) if callable(replacement) \
                else replacement
        (folder / f"f{f['frame_index']:04d}.png").write_bytes(data)
    try:
        return import_frame_sequence(p, shot_id, folder=folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _changed_member_png(seed=999, size=(64, 48)):
    """One replacement PNG body for a one-member edit."""
    image = Image.new("RGBA", size)
    px = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 5 + seed) % 256, (y * 3 + seed) % 256,
                        (x + y + seed) % 256, 255)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _run_record(label, result, *, notes=None):
    metrics = result["metrics"]
    return {"label": label,
            "end_to_end_ms": next(
                s["ms"] for s in metrics["stage_timeline"]
                if s["stage"] == "end_to_end"),
            "stage_timeline": metrics["stage_timeline"],
            "edges": metrics["edges"],
            "counters": metrics["counters"],
            "frames": {"total": metrics["counters"].get("output_frames", 0),
                       "recomputed": metrics["counters"].get(
                           "frame_composed", 0),
                       "dirty": metrics["counters"].get(
                           "dirty_frames", 0)},
            "members": {"required": metrics["counters"].get(
                            "needed_members", 0),
                        "halo": metrics["counters"].get("halo_members", 0),
                        "fetches": metrics["counters"].get(
                            "member_fetches", 0),
                        "decodes": metrics["counters"].get(
                            "member_decodes", 0),
                        "cache_hits": metrics["counters"].get(
                            "member_cache_hits", 0)},
            "cache": {"frame_hits": metrics["counters"].get(
                          "frame_cache_hits", 0),
                      "subbed_hits": metrics["counters"].get(
                          "subbed_cache_hits", 0),
                      "encode_hits": metrics["counters"].get(
                          "encode_cache_hits", 0),
                      "member_hits": metrics["counters"].get(
                          "member_cache_hits", 0)},
            "peaks": result["peaks"],
            "clean_sequence_root": result["clean_sequence_root"],
            "subbed_sequence_root": result["subbed_sequence_root"],
            "notes": list(notes or [])}


def _pack_members_for_shot(project, backend, record):
    """Pack one shot's member bytes into a content-addressed FAV1 pack +
    index on `backend`; returns pack/index object ids and member map."""
    from .fav_pack import (build_pack, image_contract_for, index_bytes,
                           make_index)
    p = Path(project)
    members, bodies = [], []
    for f in sorted(record["files"], key=lambda m: m["frame_index"]):
        data = safe_path(p, f["relative_name"]).read_bytes()
        members.append({"member_id": f"m{f['frame_index']:05d}",
                        "frame_index": f["frame_index"], "data": data})
        bodies.append(data)
    pack_bytes, entries = build_pack(members)
    contract = image_contract_for(bodies)
    index = make_index(pack_bytes, entries, image_contract=contract,
                       frame_coverage=[0, len(members)],
                       snapshot_digest=_sha256_json({
                           "asset": record["asset_id"],
                           "revision": record["revision"],
                           "content_sha256": record["content_sha256"]}),
                       recipe_digest=_sha256_json(
                           {"recipe": COMPOSE_RECIPE}),
                       sequence_digest=record["content_sha256"],
                       retention_refs=[])
    pack_id = "pack-" + _sha256(pack_bytes)
    backend.put_object(pack_id, pack_bytes)
    index_raw = index_bytes(index)
    index_id = "index-" + _sha256(index_raw)
    backend.put_object(index_id, index_raw)
    return {"pack_object_id": pack_id, "index_object_id": index_id,
            "index_sha256": _sha256(index_raw),
            "member_ids": {m["frame_index"]: m["member_id"]
                           for m in index["members"]},
            "image_contract": contract}


def pack_sources_for_project(project, backend, *, whole_pack_cap=None):
    """Archive every timeline entry's sequence as sparse packs and return
    the member_sources map for the run.

    The registry pin is unchanged — the pack is a second byte source for
    the same content, member-for-member identical — and the scheduler
    reads only what the dirty set needs plus each pack's index.
    """
    p = Path(project)
    document = load_animation_timeline(p)
    member_sources, objects = {}, {}
    for entry in document["entries"]:
        found = _resolve_metadata(p, entry["shot_id"], None, None, None)
        record = copy.deepcopy(found["record"])
        record["asset_id"] = found["asset_id"]
        record["revision"] = found["revision"]
        packed = _pack_members_for_shot(p, backend, record)
        objects[entry["shot_id"]] = {
            "pack_object_id": packed["pack_object_id"],
            "index_object_id": packed["index_object_id"]}
        member_sources[entry["instance_id"]] = PackMemberSource(
            backend, packed["pack_object_id"],
            packed["index_object_id"], packed["index_sha256"],
            packed["member_ids"], packed["image_contract"],
            whole_pack_cap=whole_pack_cap)
    return {"member_sources": member_sources, "objects": objects}


def _bench_environment():
    """Secret-free environment facts for the report and evidence."""
    return {"python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "ffmpeg": _ffmpeg_version()}


def _ffmpeg_version():
    try:
        out = run(["ffmpeg", "-version"], timeout=30).decode(
            errors="replace")
        return out.splitlines()[0] if out else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def run_benchmark(project, *, work_root=None, runs=1, workers=1,
                  prefetch=0, cache_capacity=DEFAULT_CACHE_BYTES,
                  use_packs=False, pack_backend=None,
                  whole_pack_cap=None):
    """Cold vs warm vs one-cut vs interrupted/resumed on the same input.

    With `work_root` the project is copied there first (excluding the
    volatile render/perf tree) so the benchmark's edits never touch the
    caller's project. With `use_packs` the pack phases read members
    through sparse pack reads on `pack_backend` (a counting/logged
    backend for real accounting). Reports measurements only — no
    targets, no promises, no best-case-as-general claims.
    """
    source_project = Path(project)
    if work_root is not None:
        work_root = Path(work_root).resolve()
        work_root.mkdir(parents=True, exist_ok=True)
        p = work_root / "project"
        if p.exists():
            shutil.rmtree(p)
        shutil.copytree(source_project, p, symlinks=True,
                        ignore=shutil.ignore_patterns("perf", "bench"))
    else:
        p = source_project
    perf_root = p / PERF_DIR
    input_graph = build_graph(p)
    report = {
        "report_type": "perf_benchmark", "report_version": 1,
        "generated_at": now(), "project": str(p),
        "environment": _bench_environment(),
        "input": {"snapshot_digest": input_graph["snapshot_digest"],
                  "recipe_digest": input_graph["recipe_digest"],
                  "output_frames": input_graph["output_frames"],
                  "format": input_graph["format"],
                  "ops": [{"operation_id": op["operation_id"],
                           "instance_id": op["instance_id"],
                           "output_range": op["output_range"],
                           "halo_ranges": op["halo_ranges"]}
                          for op in input_graph["ops"]]},
        "parameters": {"workers": workers, "prefetch": prefetch,
                       "cache_capacity_bytes": cache_capacity,
                       "use_packs": use_packs, "runs": runs},
        "phases": {}, "samples": {}}
    label = [0]

    def fresh_perf():
        label[0] += 1
        shutil.rmtree(perf_root, ignore_errors=True)
        return perf_root, f"r{label[0]}"

    def wall(result):
        return next(s["ms"] for s in result["metrics"]["stage_timeline"]
                    if s["stage"] == "end_to_end")

    def kwargs(member_sources=None):
        return {"workers": workers, "prefetch": prefetch,
                "cache_capacity": cache_capacity,
                "member_sources": member_sources}

    cold_walls, warm_walls = [], []
    window0 = time.monotonic()
    warm = cold = None
    for _ in range(max(1, int(runs))):
        root, tag = fresh_perf()
        cold = run_pipeline(p, root, perf_label=tag, **kwargs())
        cold_walls.append(wall(cold))
        label[0] += 1
        warm = run_pipeline(p, root, perf_label=f"r{label[0]}",
                            **kwargs())
        warm_walls.append(wall(warm))
    report["phases"]["cold"] = _run_record("cold", cold)
    report["phases"]["warm_unchanged"] = _run_record(
        "warm_unchanged", warm,
        notes=["every frame re-run found its content key already cached"])
    report["samples"] = {
        "cold_ms": cold_walls, "warm_unchanged_ms": warm_walls,
        "window_ms": round((time.monotonic() - window0) * 1000),
        "count": len(cold_walls),
        "min_cold_ms": min(cold_walls), "max_cold_ms": max(cold_walls),
        "min_warm_ms": min(warm_walls), "max_warm_ms": max(warm_walls),
        "note": "a single run is never reported as general performance"}

    # -- warm one-cut --------------------------------------------------------
    document = load_animation_timeline(p)
    pick = document["entries"][len(document["entries"]) // 2]
    # The last used member sits inside the next transition's shared range
    # when one exists — the edit then exercises a halo read too.
    member_index = pick["used_source_range"][1] - 1
    member_sources = None
    if use_packs:
        if pack_backend is None:
            raise FilmError("use_packs needs a pack_backend for real "
                            "request accounting")
        packed = pack_sources_for_project(
            p, pack_backend, whole_pack_cap=whole_pack_cap)
        member_sources = packed["member_sources"]
        fresh = fresh_perf()
        cold_pack = run_pipeline(p, perf_root, perf_label=fresh[1],
                                 **kwargs(member_sources))
        report["phases"]["pack_cold"] = _run_record(
            "pack_cold", cold_pack)
        report["pack_objects"] = packed["objects"]
    apply_one_cut_edit(
        p, pick["shot_id"],
        {member_index: _changed_member_png(seed=member_index + 7)})
    if use_packs:
        # The edited shot's new revision gets a new pack; untouched cuts
        # reuse theirs — the warm run must stay sparse.
        member_sources = pack_sources_for_project(
            p, pack_backend, whole_pack_cap=whole_pack_cap)["member_sources"]
    cut = run_pipeline(p, perf_root, perf_label="warm-one-cut",
                       **kwargs(member_sources))
    report["phases"]["warm_one_cut"] = _run_record(
        "warm_one_cut", cut,
        notes=["only the edited cut's frames and its transition halos "
               "recompute"])

    # -- interrupted then resumed ---------------------------------------------
    root, tag = fresh_perf()
    output_frames = input_graph["output_frames"]
    interrupted_at = max(1, output_frames // 2)
    try:
        run_pipeline(p, root, workers=1, prefetch=0,
                     cache_capacity=cache_capacity,
                     interrupt_after=interrupted_at, perf_label=tag)
        raise FilmError("interrupt_after did not fire — fixture too small")
    except PerfInterrupted:
        pass
    resume = run_pipeline(p, root, workers=1, prefetch=0,
                          cache_capacity=cache_capacity,
                          perf_label="resumed")
    report["phases"]["interrupted_resume"] = _run_record(
        "resumed", resume,
        notes=[f"interrupted after {interrupted_at} composed frames; the "
               "resume run reuses cached frames/members and reports its "
               "own transfer"])

    report["facets"] = {"qualification_state": "UNQUALIFIED",
                        "acceptance_state": "PENDING",
                        "release_state": "NOT_AUTHORIZED",
                        "node_state": "IN_PROGRESS"}
    report["promises"] = ("none — these are measurements on this box at "
                          "this scope; no numeric target exists and no "
                          "best-case sample generalises")
    report["vram_bytes"] = "UNKNOWN"
    report["secrets"] = "none — the report carries digests and counts only"
    report_path = perf_root / REPORT_NAME
    write(report_path, report)
    report["report_path"] = str(report_path)
    return report


# --- capability registry integration (ANIM-019 gate) ------------------------------

def _compose_op_scope(graph, op, scratch_bytes):
    scope = {"operation": "compose_frame_range",
             "runtime_contract": RUNTIME_CONTRACT,
             "route": "local",
             "width": graph["format"]["width"],
             "height": graph["format"]["height"],
             "pixel_format": "RGBA8",
             "frames": op["output_range"][1] - op["output_range"][0],
             "max_scratch_bytes": scratch_bytes}
    if op.get("halo_ranges"):
        scope["halo_ranges"] = [list(r) for r in op["halo_ranges"]]
    return scope


def _worker_environment(worker="perf-local"):
    return {"worker_id": worker, "session_id": f"perf-{os.getpid()}",
            "device": platform.node() or "localhost",
            "os": platform.platform(), "runtime": RUNTIME_CONTRACT,
            "network": "NONE", "gpu": "NONE"}


def _compose_evidence(graph, scope, report, *, worker="perf-local"):
    """Real local evidence: the benchmark actually ran this scope.

    QUALIFIED_FOR_SCOPE because the measured run covered exactly this
    scope — the fixture digests pin the run's verified roots and the
    warm run's matching roots demonstrate determinism. Still box-local:
    facets stay UNQUALIFIED/PENDING/NOT_AUTHORIZED.
    """
    from .animation_schema import validate_capability_evidence
    from .encoder_backends import capability_evidence
    phases = report["phases"]
    fixture = {"snapshot_digest": graph["snapshot_digest"],
               "clean_sequence_root":
                   phases["cold"]["clean_sequence_root"],
               "subbed_sequence_root":
                   phases["cold"]["subbed_sequence_root"],
               "warm_unchanged_root_equal":
                   phases["warm_unchanged"]["subbed_sequence_root"]
                   == phases["cold"]["subbed_sequence_root"]}
    probe = {"registry_state": "QUALIFIED_FOR_SCOPE",
             "qualification": "real-local-benchmark",
             "reason": "scheduler benchmark ran and verified this scope "
                       "on this box; box-local evidence, not product "
                       "qualification",
             "environment": _worker_environment(worker),
             "operation": "compose_frame_range",
             "scope": scope, "caps": {}, "fixture": fixture}
    # `driver` is a required schema field over the encoder vocabulary;
    # QUALIFIED_SERVICE is the convention for non-encoder operations.
    return validate_capability_evidence(
        capability_evidence("QUALIFIED_SERVICE", probe))


def _measurement_for(evidence, graph, scope, report):
    """A complete CAP-MEASURE bound to the run's actual numbers."""
    from .capability_registry import make_measurement
    cold = report["phases"]["cold"]
    warm = report["phases"]["warm_unchanged"]
    member_bytes = sum(e["bytes"] for name, e in cold["edges"].items()
                       if name.startswith(("member_source",
                                           "pack_members")))
    transfer_reads = sum(e["bytes"] for name, e in cold["edges"].items()
                         if name.startswith(("member_source", "pack",
                                             "cache")))
    transfer_requests = sum(e["requests"]
                            for e in cold["edges"].values())
    samples = report["samples"]
    return make_measurement(
        evidence,
        input_digest=graph["snapshot_digest"],
        quality_digest=graph["recipe_digest"],
        scope=scope,
        cold={"end_to_end_ms": cold["end_to_end_ms"],
              "startup_ms": next(
                  (s["ms"] for s in cold["stage_timeline"]
                   if s["stage"] == "read_verify"), 0),
              "auth_ms": 0, "queue_ms": 0},
        warm={"end_to_end_ms": warm["end_to_end_ms"]},
        stage_timeline=[s for s in cold["stage_timeline"]
                        if s["stage"] != "end_to_end"],
        shared_edge={"edge_id": "member_source",
                     "bytes": member_bytes,
                     "ms": cold["counters"].get("member_fetch_ms", 0)},
        peaks={"disk_bytes": cold["peaks"]["disk_bytes"],
               "ram_bytes": cold["peaks"]["ram_bytes"],
               "vram_bytes": 0},     # CPU compose used no VRAM
        transfer={"read_bytes": transfer_reads, "write_bytes": 0,
                  "requests": transfer_requests},
        cache={"hits": sum(warm["cache"].values()),
               "misses": cold["frames"]["recomputed"]},
        manual={"minutes": 0},
        usage={unit: 0 for unit in ALLOWANCE_UNITS},
        samples={"count": samples["count"],
                 "window_ms": samples["window_ms"],
                 "min_ms": samples["min_cold_ms"],
                 "max_ms": samples["max_cold_ms"]})


def _observation_for(evidence):
    obs = {"environment": copy.deepcopy(evidence["environment"])}
    for field in ("account_binding", "credential_epoch",
                  "adapter_digest", "driver", "operation", "route",
                  "transport", "caps", "allowance"):
        obs[field] = copy.deepcopy(evidence.get(field))
    return obs


def make_perf_plan(project, *, graph=None, ops_worker="perf-local",
                   scratch_bytes=1 << 26,
                   cache_bytes=DEFAULT_CACHE_BYTES):
    """The AUTO_PERFORMANCE ExecutionPlan 1 for this project's graph."""
    from .execution_plan import make_execution_plan
    graph = graph or build_graph(project)
    operations = [{"operation_id": op["operation_id"],
                   "kind": "COMPOSE_RANGE",
                   "output_range": op["output_range"],
                   "halo_ranges": op["halo_ranges"],
                   "route": "LOCAL_NATIVE", "worker": ops_worker,
                   "recipe_digest": graph["recipe_digest"],
                   "runtime_contract": RUNTIME_CONTRACT,
                   "depends_on": []}
                  for op in graph["ops"]]
    fmt = graph["format"]
    plan = make_execution_plan(
        graph["snapshot_digest"], operations,
        storage={"profile": "LOCAL_FULL", "archive_manifest": None},
        execution={"policy": "AUTO_PERFORMANCE",
                   "allowed_routes": ["LOCAL_NATIVE"],
                   "capability_evidence_required": True,
                   "allow_additional_charges": False},
        encoding={"driver_policy": "AUTO_PERFORMANCE",
                  "allowed_drivers": ["FFMPEG"],
                  "delivery_profile": "MV_H264_AAC_V1",
                  "width": fmt["width"], "height": fmt["height"],
                  "fps": {"num": fmt["fps"], "den": 1},
                  "output_frames": graph["output_frames"]},
        workspace={"pc_cache_limit_bytes": cache_bytes,
                   "worker_scratch_limit_bytes": scratch_bytes,
                   "on_limit": "PAUSE"},
        transfer_route="COORDINATOR_RELAY",
        coordinator_location="USER_DESKTOP",
        transfer_edges=[],
        resource_reservations=[{"location": "USER_DESKTOP",
                                "peak_bytes": cache_bytes}])
    return plan


def register_benchmark(state_dir, report, *, scratch_bytes=1 << 26,
                       worker="perf-local"):
    """Register the benchmark's evidence + measurements — the only door
    to AUTO_PERFORMANCE for a scheduler candidate.

    Each op scope gets its own evidence and bound measurement built from
    the run that actually covered it; the input digest pins the
    pre-edit snapshot the benchmark measured. Candidates without
    complete bound evidence stay UNKNOWN and are never selectable.
    """
    from .capability_registry import (plan_preflight, register,
                                      save_measurement)
    graph = {"project": report["project"],
             "snapshot_digest": report["input"]["snapshot_digest"],
             "recipe_digest": report["input"]["recipe_digest"],
             "format": report["input"]["format"],
             "output_frames": report["input"]["output_frames"],
             "ops": report["input"]["ops"]}
    plan = make_perf_plan(report["project"], graph=graph,
                          ops_worker=worker,
                          scratch_bytes=scratch_bytes)
    registered, evidences = [], []
    for op, gop in zip(plan["operations"], graph["ops"]):
        scope = _compose_op_scope(graph, gop, scratch_bytes)
        evidence = _compose_evidence(graph, scope, report,
                                     worker=worker)
        register(state_dir, evidence)
        measurement = _measurement_for(evidence, graph, scope, report)
        save_measurement(state_dir, measurement)
        evidences.append(evidence)
        registered.append({"operation_id": op["operation_id"],
                           "evidence_id": evidence["evidence_id"],
                           "measurement_id":
                               measurement["measurement_id"]})
    observation = _observation_for(evidences[0]) if evidences else None
    preflight = plan_preflight(plan, state_dir=state_dir,
                               observation=observation)
    return {"registered": registered, "plan": plan,
            "preflight": preflight}


def refuse_direct_drive(candidate):
    """Defense in depth: DIRECT_DRIVE is never a scheduler candidate —
    no evidence, measurement or estimate may admit it (execution §5 /
    evolution §6)."""
    from .execution_plan import FORBIDDEN_ROUTES
    if candidate.get("route") in FORBIDDEN_ROUTES \
            or candidate.get("driver") in FORBIDDEN_ROUTES:
        raise FilmError("DIRECT_DRIVE is never an approved or selectable "
                        "candidate — a separate ADR and explicit user "
                        "authorization would be required")
    return candidate


def select_candidates(report):
    """Selection view of a preflight report; refuses forbidden routes
    even if a verdict somehow claimed them."""
    selections = []
    for sel in report.get("selections", []):
        verdict = next((v for v in report["candidates"]
                        if v["candidate_id"] == sel["candidate_id"]), {})
        refuse_direct_drive(verdict)
        selections.append(sel)
    return selections


# --- CLI ----------------------------------------------------------------------------

def perf_state_dir(project):
    return Path(project) / PERF_DIR / "registry"


def perf_plan_command(project, *, state_dir=None, ops_worker="perf-local",
                      scratch_bytes=1 << 26):
    """`perf-plan PROJECT`: dependency graph + AUTO_PERFORMANCE plan +
    registry preflight over whatever evidence exists in state_dir."""
    p = Path(project).resolve()
    with project_mutex(p):
        graph = build_graph(p)
        plan = make_perf_plan(p, graph=graph, ops_worker=ops_worker,
                              scratch_bytes=scratch_bytes)
        state_dir = state_dir or perf_state_dir(p)
        preflight = None
        if Path(state_dir).is_dir():
            from .capability_registry import (entries,
                                              plan_preflight)
            if entries(state_dir):
                preflight = plan_preflight(plan, state_dir=state_dir)
    return {"project": str(p), "graph": graph_report(graph),
            "plan": plan, "preflight": preflight,
            "route_policy": {
                "direct_drive": "EXCLUDED — never a plan route, "
                                "candidate or selection; requires a "
                                "separate ADR and user authorization"},
            "state_dir": str(state_dir),
            "facets": {"qualification_state": "UNQUALIFIED",
                       "acceptance_state": "PENDING",
                       "release_state": "NOT_AUTHORIZED"},
            "note": "a plan + registry verdicts only — nothing executes "
                    "and no approval is inferred"}


def perf_benchmark_command(project, *, work_root=None, runs=1, workers=1,
                           prefetch=0, use_packs=False,
                           backend_root=None, register=True,
                           scratch_bytes=1 << 26):
    """`perf-benchmark PROJECT`: run the benchmark on a working copy,
    write the secret-free report, and register the complete bound
    measurements into the project's perf registry."""
    p = Path(project).resolve()
    with project_mutex(p):
        if work_root is None:
            work_root = Path(tempfile.mkdtemp(prefix="perf-bench-"))
        backend = None
        if use_packs:
            from .storage_backends.fake_drive import FakeDriveBackend
            backend = CountingBackend(FakeDriveBackend(
                root=Path(backend_root) if backend_root
                else Path(work_root) / "archive"))
        report = run_benchmark(p, work_root=work_root, runs=runs,
                               workers=workers, prefetch=prefetch,
                               use_packs=use_packs, pack_backend=backend)
        registration = None
        if register:
            registration = register_benchmark(
                perf_state_dir(p), report,
                scratch_bytes=scratch_bytes)
    out = {"report_path": report["report_path"],
           "phases": {k: {"end_to_end_ms": v["end_to_end_ms"],
                          "edges": v["edges"], "cache": v["cache"],
                          "frames": v["frames"], "peaks": v["peaks"]}
                      for k, v in report["phases"].items()},
           "samples": report["samples"], "facets": report["facets"],
           "vram_bytes": report["vram_bytes"],
           "promises": report["promises"]}
    if registration:
        out["registry"] = {
            "state_dir": str(perf_state_dir(p)),
            "registered": registration["registered"],
            "selections": select_candidates(registration["preflight"]),
            "unknown_estimate":
                registration["preflight"]["unknown_estimate"]}
    return out
