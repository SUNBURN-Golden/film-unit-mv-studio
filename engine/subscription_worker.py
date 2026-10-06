"""FILM UNIT subscription worker script (ANIM-016).

This file is the self-contained worker the app ships inside a manual
execution packet (exec/storage design 7.3). A user runs it *inside* the
subscription service's own UI — a code interpreter, a hosted notebook —
with no install step:

    python film_unit_worker.py <packet_dir> <result_dir>

Hard contract:

- standard library only — no network, no GPU, no third-party packages,
  no downloads, no eval/exec of fetched code;
- reads only the packet's declared inputs, writes only the result dir;
- output is PNG frames plus a `result_manifest.json` and a `receipt.json`
  bound to the packet's job/attempt/request identity — digests only, no
  credentials or tokens can exist here because the packet carries none.

`WORKER_SCRIPT_VERSION` and the file's SHA-256 are pinned into every
packet; the coordinator rejects a result produced by different bytes.
"""
from datetime import datetime, timezone
import hashlib
import json
import platform
import struct
import sys
import zlib
from pathlib import Path

WORKER_SCRIPT_VERSION = 1

# The deterministic frame recipe the ExecutionPlan pins. It is duplicated
# here on purpose: the script must run where the `engine` package cannot
# be imported. Any drift is caught by the coordinator's independent
# re-derivation (a worker COMPLETE is never trusted) and by the test that
# compares this copy against engine.execution_workers.render_frame_bytes.


def _canon(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False)
            + "\n").encode("utf-8")


def render_frame_bytes(frame_index, contract, snapshot_digest, recipe_digest):
    size = contract["stride"] * contract["height"]
    seed = hashlib.sha256(_canon({
        "recipe_digest": recipe_digest,
        "snapshot_digest": snapshot_digest,
        "frame_index": frame_index,
        "width": contract["width"], "height": contract["height"],
        "pixel_format": contract["pixel_format"],
        "color_space": contract["color_space"],
        "transfer": contract["transfer"]})).digest()
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:size])


_CHANNELS = {"RGBA8": (6, 4), "RGB8": (2, 3), "GRAY8": (0, 1)}


def png_bytes(width, height, pixel_format, data):
    """Minimal PNG writer — filter-0 scanlines, stdlib zlib only."""
    color_type, channels = _CHANNELS[pixel_format]
    stride = width * channels
    raw = b"".join(b"\x00" + data[y * stride:(y + 1) * stride]
                   for y in range(height))

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _fail(message, code=1):
    print(f"film-unit worker: {message}", file=sys.stderr)
    return code


def run_packet(packet_dir, result_dir):
    packet_dir = Path(packet_dir)
    result_dir = Path(result_dir)
    packet = json.loads((packet_dir / "packet.json").read_text("utf-8"))
    if packet.get("document_type") != "subscription_packet":
        return _fail("packet.json is not a subscription_packet document")
    if packet.get("limits", {}).get("network") != "NONE":
        return _fail("packet does not declare a no-network runtime")
    if packet.get("limits", {}).get("gpu") != "NONE":
        return _fail("this worker has no GPU path")
    contract = packet["frame_contract"]
    fmt = contract["pixel_format"]
    if fmt not in _CHANNELS:
        return _fail(f"unsupported pixel format {fmt}")
    if contract["stride"] != contract["width"] * _CHANNELS[fmt][1]:
        return _fail("contract stride must equal width * channels")
    # Verify every declared input before doing any work: the run is bound
    # to the pinned input set, not whatever happens to be staged. No
    # input may escape the packet dir or arrive through a symlink.
    packet_root = packet_dir.resolve()
    for entry in packet["inputs"]["files"]:
        rel = Path(entry["path"])
        if rel.is_absolute() or ".." in rel.parts:
            return _fail(f"input {entry['path']} escapes the packet dir")
        path = packet_dir / entry["path"]
        if path.is_symlink():
            return _fail(f"input {entry['path']} is a symlink")
        resolved = path.resolve()
        if not resolved.is_relative_to(packet_root):
            return _fail(f"input {entry['path']} escapes the packet dir")
        if not resolved.is_file():
            return _fail(f"missing input {entry['path']}")
        if hashlib.sha256(resolved.read_bytes()).hexdigest() \
                != entry["sha256"]:
            return _fail(f"input {entry['path']} hash mismatch")
    start, end = packet["execution_unit"]["frame_range"]
    outputs = []
    out_dir = result_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    produced_at = datetime.now(timezone.utc).isoformat()
    unit = packet["execution_unit"]
    session_epoch = packet["entitlement"]["credential_epoch"]
    members = []
    for index in range(start, end):
        data = render_frame_bytes(index, contract,
                                  packet["plan"]["snapshot_digest"],
                                  packet["plan"]["recipe_digest"])
        name = f"frame_{index:06d}.png"
        body = png_bytes(contract["width"], contract["height"], fmt, data)
        (out_dir / name).write_bytes(body)
        pixel_sha256 = hashlib.sha256(data).hexdigest()
        members.append({"frame_index": index, "sha256": pixel_sha256})
        outputs.append({"member_id": f"frame-{index:06d}",
                        "frame_index": index, "file": f"outputs/{name}",
                        "file_sha256": hashlib.sha256(body).hexdigest(),
                        "pixel_sha256": pixel_sha256})
    receipt = {"receipt_id": "rcpt-" + hashlib.sha256(_canon(
                   {"packet_id": packet["packet_id"],
                    "kind": "COMPLETE"})).hexdigest()[:16],
               "actor": packet["receipt"]["actor"],
               "job_key": unit["job_key"],
               "attempt_id": unit["attempt_id"],
               "request_id": unit["request_id"],
               "snapshot_digest": packet["plan"]["snapshot_digest"],
               "plan_revision": packet["plan"]["plan_revision"],
               "covered_range": [start, end],
               "nonce": packet["receipt"]["nonce_digest"],
               "kind": "COMPLETE",
               "members": members,
               "grant_digest": packet["receipt"]["grant_digest"],
               "evidence": packet["receipt"]["evidence"]}
    manifest = {"document_type": "subscription_result", "schema_version": 1,
                "packet_id": packet["packet_id"],
                "job_key": unit["job_key"],
                "attempt_id": unit["attempt_id"],
                "request_id": unit["request_id"],
                "snapshot_digest": packet["plan"]["snapshot_digest"],
                "plan_revision": packet["plan"]["plan_revision"],
                "actor": packet["receipt"]["actor"],
                # provenance is the bytes that actually ran, hashed at
                # run time — never the packet's echoed claim
                "worker_script_sha256": hashlib.sha256(
                    Path(__file__).resolve().read_bytes()).hexdigest(),
                "worker_script_version": WORKER_SCRIPT_VERSION,
                "session_epoch": session_epoch,
                "frame_range": [start, end],
                "outputs": outputs,
                "receipt_nonce": packet["receipt"]["nonce_digest"],
                "grant_digest": packet["receipt"]["grant_digest"],
                "state": "COMPLETE", "error": None,
                "produced_at": produced_at,
                "environment": {
                    "python": sys.version.split()[0],
                    "os": platform.system().lower() + "-" + platform.release(),
                    "device": platform.machine(),
                    "runtime": "python-stdlib",
                    "network": "NONE", "gpu": "NONE"}}
    (result_dir / "result_manifest.json").write_bytes(_canon(manifest))
    (result_dir / "receipt.json").write_bytes(_canon(receipt))
    print(f"film-unit worker: wrote {len(outputs)} frames "
          f"[{start}, {end}) to {result_dir}")
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: film_unit_worker.py <packet_dir> <result_dir>",
              file=sys.stderr)
        return 2
    return run_packet(args[0], args[1])


if __name__ == "__main__":
    raise SystemExit(main())
