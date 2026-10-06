"""CLI-facing journal status/reconciliation surface (ANIM-021).

`journal-status` and `journal-reconcile` operate on a coordinator state
dir holding `job_journal.jsonl`: the first reports the chain health and
the rebuilt job picture; the second rebuilds the durable runtime
snapshot from the journal and lists every job whose resolution still
needs an explicit worker reconcile. `archive-seal-reconcile` replays a
commit dir's journal against a local archive root and resolves
MANIFEST_INTENT / SEAL_UNKNOWN commits — the same manifest bytes under
the same build identity, never a duplicate. None of these run an
automatic retry or a polling loop.
"""
from pathlib import Path

from .core import FilmError
from .durable_journal import load_journal
from .execution_workers import Coordinator
from .archive_commit import seal_reconcile
from .storage_backends.local import LocalArchiveBackend


def journal_status(state_dir):
    """Journal chain health + rebuilt job picture for one state dir."""
    path = Path(state_dir)
    if not path.is_dir():
        raise FilmError(f"No coordinator state dir: {state_dir}")
    loaded = load_journal(path / Coordinator.JOURNAL_NAME)
    coordinator = Coordinator(path)
    report = coordinator.journal_status()
    report["journal_file"] = {
        "records": len(loaded["records"]), "head": loaded["head"],
        "tail": loaded["tail"], "tail_reason": loaded["tail_reason"]}
    report["qualification_state"] = "UNQUALIFIED"
    return report


def journal_reconcile(state_dir):
    """Rebuild runtime_state.json from the journal and report unresolved
    jobs — the only action is the explicit per-job reconcile the caller
    still has to run against its worker."""
    path = Path(state_dir)
    if not path.is_dir():
        raise FilmError(f"No coordinator state dir: {state_dir}")
    coordinator = Coordinator(path)
    report = coordinator.journal_status()
    if not coordinator.journal_fenced:
        # The derived snapshot is refreshed from the authoritative chain.
        coordinator._persist()
    report["state_file_written"] = not coordinator.journal_fenced
    return report


def archive_seal_reconcile(commit_dir, root, *, upload=None, retry=None):
    """Resolve MANIFEST_INTENT / SEAL_UNKNOWN commits on a local root."""
    commit_dir, root = Path(commit_dir), Path(root)
    if not commit_dir.is_dir():
        raise FilmError(f"No archive commit dir: {commit_dir}")
    if not root.is_dir():
        raise FilmError(f"No archive backend root: {root}")
    backend = LocalArchiveBackend(root)
    report = seal_reconcile(commit_dir, backend, upload=upload,
                            retry=retry)
    report["qualification_state"] = "UNQUALIFIED"
    return report
