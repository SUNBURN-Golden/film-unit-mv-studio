"""QUALIFIED_SERVICE encoder driver (ANIM-015).

Protocol-only — no network, no credential, no account state. The driver
materializes a `manual_export` packet containing the encoded-packet
contract (encode_digest scope, frame sequence root, required deliverable
shape); an external party returns the packets, which are then muxed and
verified by exactly the same MediaVerifier path as every other driver.

The resting state is DOCUMENTED_ONLY: the protocol is defined, and an
imported result becomes QUALIFIED_FOR_SCOPE evidence only after the
verifier actually passed it.
"""
from pathlib import Path

from ..animation_schema import write_canon
from ..core import FilmError, digest
from . import EncoderDriver, FrameSource, ManualExportRequired


class QualifiedServiceDriver(EncoderDriver):
    name = "QUALIFIED_SERVICE"
    manual_protocol = True

    def codec_contract(self, fmt):
        return "service:manual-export", {
            "mode": "profile-defined",
            "profile": "MV_H264_AAC_V1"}

    def probe(self, scope=None):
        return {
            "environment": {"transport": "none", "network": "none",
                            "account": "none",
                            "mode": "protocol-only manual export/import"},
            "registry_state": "DOCUMENTED_ONLY",
            "qualification": "protocol-only",
            "reason": "the manual export/import protocol is implemented; "
                      "nothing here proves an external encoder can "
                      "produce packets for this scope"}

    def encode(self, frames: FrameSource, recipe, work_dir):
        work_dir = Path(work_dir)
        packet = {
            "document_type": "manual_export",
            "schema_version": 1,
            "driver": "QUALIFIED_SERVICE",
            "frame_count": frames.count,
            "width": frames.width, "height": frames.height,
            "fps": {"num": frames.fps, "den": 1},
            "frames_root": None,  # filled below
            "required": {
                "codec": "h264",
                "pixel_format": recipe["pixel_format"],
                "container": "mp4 or reordering-free elementary stream",
                "timing": f"explicit {frames.fps}/1 PTS, frame i at "
                          f"i/{frames.fps}s"},
            "verify_contract": "the same MediaVerifier as every driver — "
                               "it, not the service, is the authority"}
        import hashlib
        from ..media_verify import frame_pixel_sha256
        packet["frames_root"] = hashlib.sha256(
            "".join(frame_pixel_sha256(frames.path(i))
                    for i in range(frames.count)).encode()).hexdigest()
        write_canon(work_dir / "manual_export_packet.json", packet)
        raise ManualExportRequired(
            "QUALIFIED_SERVICE cannot produce packets locally; a manual "
            "export packet was written to "
            f"{work_dir / 'manual_export_packet.json'}. Import the service "
            "result with `film encode-build --import` after the external "
            "party returns encoded packets.", packet=packet)

    @staticmethod
    def import_packets(import_path, frames: FrameSource, work_dir):
        """Validate a manually imported service result: real bytes, real
        H.264 — anything else is rejected before muxing."""
        work_dir = Path(work_dir)
        source = Path(import_path)
        if not source.is_file() or source.stat().st_size < 32:
            raise FilmError("Imported service result is missing or empty")
        from ..core import probe
        try:
            meta = probe(source)
        except FilmError:
            raise FilmError("Imported service result is not a media file")
        streams = [s for s in meta["streams"]
                   if s["codec_type"] == "video"]
        if not streams or streams[0]["codec_name"] != "h264":
            raise FilmError("Imported service result must contain an "
                            "H.264 video stream")
        dest = work_dir / "imported_packets.mp4"
        dest.write_bytes(source.read_bytes())
        return {"path": dest, "timed": True, "container": "mp4",
                "packets": "h264", "import_sha256": digest(source)}
