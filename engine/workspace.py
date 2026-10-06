"""Bounded cache/workspace for archive restore (ANIM-013, storage §4.2).

The workspace owns one cache directory with a hard byte cap. Entries carry
a retention class; eviction walks from most-temporary to most-pinned and a
`PINNED` entry can never be evicted — a request that would need to evict a
pin fails with WORKSPACE_PINNED_FULL instead. The decoded-byte cap is a RAM
budget independent of compressed PNG size: `decode_budget` rejects a member
whose decoded raster would exceed it before any decoder allocation.

Restore never writes into the destination directly. Bytes land in a staged
generation directory counted against the same cap; only after every member
is fetched, hash-verified and technically checked does `commit` publish the
staged files into an allowed root under application-generated names. An
interrupted restore leaves verified members in the cache (class
VERIFIED_MEMBER) so a resume does not re-fetch them.
"""
from pathlib import Path
import os
import re
import shutil
import tempfile
import time
import uuid

from .core import FilmError
from .fav_pack import check_member_png

# Ordered most-kept -> most-temporary. Eviction scans from the end.
RETENTION_ORDER = ["PINNED", "VERIFIED_MEMBER", "PACK_READBACK",
                   "RESTORE_WORKING", "TEMPORARY"]
RETENTION = {name: rank for rank, name in enumerate(RETENTION_ORDER)}


class WorkspaceEntry:
    def __init__(self, key, path, size, retention, pinned=False):
        self.key = key
        self.path = Path(path)
        self.size = size
        self.retention = retention
        self.pinned = pinned
        self.last_used = time.monotonic()


class Workspace:
    """A byte-bounded cache directory with retention classes and staging."""

    def __init__(self, root, capacity_bytes, decoded_byte_cap=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        self.capacity_bytes = capacity_bytes
        self.decoded_byte_cap = decoded_byte_cap
        self.entries = {}
        self._stage_claims = 0
        # Staged generations from a crashed restore are never published.
        shutil.rmtree(self.root / ".staged", ignore_errors=True)
        self._scan()

    def _scan(self):
        """Re-attach cache files that survived an earlier process."""
        for path in sorted((self.root / "objects").iterdir()):
            if path.is_file():
                key = path.name
                self.entries[key] = WorkspaceEntry(
                    key, path, path.stat().st_size, "VERIFIED_MEMBER")

    # -- accounting --------------------------------------------------------
    @property
    def used_bytes(self):
        return sum(e.size for e in self.entries.values()) + self._stage_claims

    @property
    def available_bytes(self):
        return self.capacity_bytes - self.used_bytes

    def _evictable(self):
        return [e for e in self.entries.values() if not e.pinned
                and e.retention != "PINNED"]

    def evict_one(self):
        """Drop the most-temporary, least-recently-used unpinned entry."""
        candidates = self._evictable()
        if not candidates:
            return None
        victim = max(candidates,
                     key=lambda e: (RETENTION[e.retention], -e.last_used))
        victim.path.unlink(missing_ok=True)
        del self.entries[victim.key]
        return victim.key

    def _ensure(self, needed):
        while self.available_bytes < needed:
            if self.evict_one() is None:
                raise FilmError(
                    "WORKSPACE_FULL: the byte cap is reached and every "
                    "remaining entry is pinned")

    # -- entries ------------------------------------------------------------
    def put(self, key, data, retention="TEMPORARY", pinned=False):
        """Store bytes under a caller key; identical re-put is idempotent."""
        if retention not in RETENTION:
            raise FilmError(f"Unknown retention class: {retention}")
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", key):
            raise FilmError(f"Unsafe cache key: {key!r}")
        existing = self.entries.get(key)
        if existing is not None and existing.path.is_file() \
                and existing.path.read_bytes() == data:
            existing.last_used = time.monotonic()
            existing.retention = retention
            existing.pinned = existing.pinned or pinned
            return existing
        if existing is not None:
            existing.path.unlink(missing_ok=True)
            del self.entries[key]
        self._ensure(len(data))
        fd, temp = tempfile.mkstemp(dir=self.root / "objects", prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            target = self.root / "objects" / key
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
        entry = WorkspaceEntry(key, target, len(data), retention, pinned)
        self.entries[key] = entry
        return entry

    def get(self, key):
        entry = self.entries.get(key)
        if entry is None or not entry.path.is_file():
            return None
        entry.last_used = time.monotonic()
        return entry.path.read_bytes()

    def has(self, key):
        entry = self.entries.get(key)
        return entry is not None and entry.path.is_file()

    def pin(self, key):
        entry = self.entries.get(key)
        if entry is None:
            raise FilmError(f"Cannot pin a missing cache entry: {key}")
        entry.pinned = True

    def unpin(self, key):
        entry = self.entries.get(key)
        if entry is not None:
            entry.pinned = False

    def drop(self, key):
        entry = self.entries.pop(key, None)
        if entry is not None:
            entry.path.unlink(missing_ok=True)

    def decoded_budget(self, needed):
        """RAM cap check, independent of compressed size."""
        if self.decoded_byte_cap is not None \
                and needed > self.decoded_byte_cap:
            raise FilmError(
                f"Decoded raster {needed} bytes exceeds the workspace "
                f"decode cap {self.decoded_byte_cap}; refusing allocation")
        return True

    def check_member(self, data, image_contract, member_id):
        """Contract gate + RAM budget for one pack member."""
        estimate = check_member_png(data, image_contract, member_id)
        self.decoded_budget(estimate)
        return estimate

    # -- restore staging -----------------------------------------------------
    def begin_restore(self, reserve_bytes=0):
        """Open a staged generation; `reserve_bytes` pre-books peak space."""
        self._ensure(reserve_bytes)
        stage = self.root / ".staged" / f"restore-{uuid.uuid4().hex[:8]}"
        stage.mkdir(parents=True)
        self._stage_claims += reserve_bytes
        return RestoreStage(self, stage, reserve_bytes)


class RestoreStage:
    """Staged restore files; invisible to the destination until commit."""

    def __init__(self, workspace, stage_dir, reserve_bytes):
        self.workspace = workspace
        self.dir = Path(stage_dir)
        self._claimed = reserve_bytes     # total bytes booked for this stage
        self._reserved = reserve_bytes    # headroom left inside the booking
        self._closed = False

    def _charge(self, data_len):
        claim = max(0, data_len - self._reserved)
        if claim:
            self.workspace._ensure(claim)
            self.workspace._stage_claims += claim
            self._claimed += claim
        self._reserved = max(0, self._reserved - data_len)

    def write_member(self, name, data):
        if self._closed:
            raise FilmError("Restore stage is closed")
        self._charge(len(data))
        fd, temp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temp, self.dir / name)
        finally:
            Path(temp).unlink(missing_ok=True)
        return self.dir / name

    def _release(self):
        self.workspace._stage_claims -= self._claimed
        self._claimed = 0
        self._reserved = 0
        self._closed = True

    def abort(self):
        """Drop the staged generation; cached verified members survive."""
        if self._closed:
            return
        self._release()
        for f in self.dir.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
        try:
            self.dir.rmdir()
        except OSError:
            pass

    def commit(self, dest_dir):
        """Publish staged members; refuses any pre-existing destination."""
        if self._closed:
            raise FilmError("Restore stage is closed")
        dest_dir = Path(dest_dir)
        staged = sorted(f for f in self.dir.iterdir() if f.is_file())
        for f in staged:
            target = dest_dir / f.name
            if target.exists() or target.is_symlink():
                self.abort()
                raise FilmError(
                    f"RESTORE_OVERWRITE_BLOCKED: {target} already exists; "
                    "restores never overwrite existing outputs")
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in staged:
            os.replace(f, dest_dir / f.name)
        self._release()
        self.dir.rmdir()
        return sorted(f.name for f in staged)
