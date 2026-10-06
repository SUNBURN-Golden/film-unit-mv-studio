"""FAV1 seekable PNG pack and pack_index contracts (ANIM-013).

Schema ADR §10.1-10.2: a pack is `FAV1PACK` magic, uint32-LE version 1 and
uint32-LE header byte length (16), then the original PNG member bytes in
index order — no padding, no outer compression. The sidecar `pack_index`
document (`document_type: pack_index`, `schema_version: 1`, CANON_JSON_V1)
pins the pack's whole-byte hash, length and header bounds plus each member's
id, frame index or source role, byte offset, byte length and SHA-256.

The index is validated before any member is decoded: pinned index hash,
offset/length overflow, pack bounds, header intrusion, overlap, gaps,
duplicate member ids and duplicate delivery frame indices, member order and
coverage. A member's PNG signature and IHDR dimensions are checked against
the index's `image_contract` — including the encoded/decoded byte caps —
before any decoder runs. A corrupted member is rejected; the source bytes
are never deleted.
"""
from pathlib import Path
import hashlib
import json
import struct

from .animation_schema import canon_bytes, check_document, read_canon
from .core import FilmError

PACK_MAGIC = b"FAV1PACK"
PACK_FORMAT_VERSION = 1
HEADER_BYTE_LENGTH = 16
# A sidecar index larger than 16 MiB is refused before any member decode.
INDEX_MAX_BYTES = 16777216
SHA256_LEN = 64

INDEX_TYPE = "pack_index"
INDEX_FIELDS = {"document_type", "schema_version", "pack_format_version",
                "pack_sha256", "pack_byte_length", "header_byte_length",
                "snapshot_digest", "recipe_digest", "sequence_digest",
                "members", "image_contract", "member_count", "frame_coverage",
                "retention_refs"}
MEMBER_FIELDS = {"member_id", "frame_index", "source_role", "byte_offset",
                 "byte_length", "sha256"}
CONTRACT_FIELDS = {"width", "height", "pixel_format", "alpha", "color",
                   "max_encoded_bytes", "max_decoded_bytes"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_FORMAT = {(0, 8): "GRAY8", (2, 8): "RGB8", (3, 8): "INDEX8",
               (4, 8): "GRAY_ALPHA8", (6, 8): "RGBA8"}


def _sha(value, what):
    if value is not None and (type(value) is not str
                              or len(value) != SHA256_LEN
                              or any(c not in "0123456789abcdef" for c in value)):
        raise FilmError(f"{what} must be a lowercase SHA-256 or null")
    return value


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def png_header(data, name="member"):
    """PNG signature + IHDR dimensions, read before any decode allocation."""
    if len(data) < 33 or data[:8] != PNG_SIGNATURE:
        raise FilmError(f"Not a PNG member: {name}")
    ihdr_length = int.from_bytes(data[8:12], "big")
    if data[12:16] != b"IHDR" or ihdr_length != 13:
        raise FilmError(f"Malformed PNG IHDR: {name}")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    bit_depth, color_type = data[24], data[25]
    if width < 1 or height < 1 or color_type not in _PNG_CHANNELS:
        raise FilmError(f"Unsupported PNG header in member: {name}")
    if (color_type, bit_depth) not in _PNG_FORMAT:
        raise FilmError(f"Unsupported PNG bit depth/format in member: {name}")
    return {"width": width, "height": height, "bit_depth": bit_depth,
            "color_type": color_type,
            "pixel_format": _PNG_FORMAT[(color_type, bit_depth)]}


def decoded_bytes_estimate(header):
    """Inflated raster size for the image_contract decode cap."""
    channels = _PNG_CHANNELS[header["color_type"]]
    return header["width"] * header["height"] * channels * (header["bit_depth"] // 8)


def image_contract_for(png_bodies, name="sequence"):
    """Derive one image_contract from a set of member PNG bytes.

    All members must share canvas and pixel format; caps are the actual
    maximum encoded length and the computed decoded size — not promises
    about headroom for other images.
    """
    if not png_bodies:
        raise FilmError("image_contract needs at least one member")
    header = png_header(png_bodies[0], name)
    for position, body in enumerate(png_bodies[1:], 1):
        other = png_header(body, f"{name}[{position}]")
        if (other["width"], other["height"], other["pixel_format"]) \
                != (header["width"], header["height"], header["pixel_format"]):
            raise FilmError("Pack members must share one canvas and pixel format")
    return {"width": header["width"], "height": header["height"],
            "pixel_format": header["pixel_format"],
            "alpha": header["color_type"] in {4, 6},
            "color": "sRGB",
            "max_encoded_bytes": max(len(b) for b in png_bodies),
            "max_decoded_bytes": decoded_bytes_estimate(header)}


def check_member_png(data, image_contract, member_id):
    """Member PNG pre-decode gate: contract geometry and byte caps.

    Returns the decoded-byte estimate the caller may budget against its RAM
    reservation; a member that lies about its dimensions is rejected before
    any decoder allocation happens.
    """
    header = png_header(data, member_id)
    if header["width"] != image_contract["width"] \
            or header["height"] != image_contract["height"]:
        raise FilmError(f"Member {member_id} PNG dimensions do not match "
                        "image_contract; refusing decode")
    if header["pixel_format"] != image_contract["pixel_format"]:
        raise FilmError(f"Member {member_id} pixel format does not match "
                        "image_contract; refusing decode")
    if len(data) > image_contract["max_encoded_bytes"]:
        raise FilmError(f"Member {member_id} exceeds max_encoded_bytes")
    estimate = decoded_bytes_estimate(header)
    if estimate > image_contract["max_decoded_bytes"]:
        raise FilmError(f"Member {member_id} decoded size exceeds "
                        "max_decoded_bytes; refusing allocation")
    return estimate


def build_pack(members):
    """Pack member dicts {member_id, frame_index|source_role, data: bytes}.

    Returns `(pack_bytes, member_entries)` where entries carry the fixed
    index order and the byte ranges that make the pack seekable.
    """
    if not members:
        raise FilmError("A pack needs at least one member")
    header = PACK_MAGIC + struct.pack("<II", PACK_FORMAT_VERSION,
                                      HEADER_BYTE_LENGTH)
    offset = HEADER_BYTE_LENGTH
    entries, bodies = [], []
    seen_ids, seen_frames = set(), set()
    for member in members:
        data = member["data"]
        member_id = member["member_id"]
        if member_id in seen_ids:
            raise FilmError(f"Duplicate member id: {member_id}")
        seen_ids.add(member_id)
        frame_index = member.get("frame_index")
        if frame_index is not None:
            if frame_index in seen_frames:
                raise FilmError(f"Duplicate frame_index: {frame_index}")
            seen_frames.add(frame_index)
        entries.append({"member_id": member_id,
                        "frame_index": frame_index,
                        "source_role": member.get("source_role"),
                        "byte_offset": offset, "byte_length": len(data),
                        "sha256": sha256_bytes(data)})
        bodies.append(data)
        offset += len(data)
    return header + b"".join(bodies), entries


def make_index(pack_bytes, member_entries, *, image_contract, frame_coverage,
               snapshot_digest, recipe_digest, sequence_digest, retention_refs):
    """The sidecar pack_index document pinning the built pack."""
    doc = {"document_type": INDEX_TYPE, "schema_version": 1,
           "pack_format_version": PACK_FORMAT_VERSION,
           "pack_sha256": sha256_bytes(pack_bytes),
           "pack_byte_length": len(pack_bytes),
           "header_byte_length": HEADER_BYTE_LENGTH,
           "snapshot_digest": snapshot_digest,
           "recipe_digest": recipe_digest,
           "sequence_digest": sequence_digest,
           "members": member_entries,
           "image_contract": image_contract,
           "member_count": len(member_entries),
           "frame_coverage": list(frame_coverage),
           "retention_refs": list(retention_refs)}
    return validate_index(doc)


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def validate_index(document):
    """Structural + bounds contract of a pack_index document (schema §10.2)."""
    check_document(document, INDEX_TYPE)
    if set(document.keys()) - INDEX_FIELDS:
        raise FilmError("Unknown pack_index fields")
    if document["pack_format_version"] != PACK_FORMAT_VERSION:
        raise FilmError("Unsupported pack_format_version")
    if document["header_byte_length"] != HEADER_BYTE_LENGTH:
        raise FilmError("Unsupported pack header_byte_length")
    pack_length = _int(document["pack_byte_length"], "pack_byte_length",
                       HEADER_BYTE_LENGTH + 1)
    _sha(document.get("pack_sha256"), "pack_sha256")
    for field in ("snapshot_digest", "recipe_digest", "sequence_digest"):
        _sha(document.get(field), field)
    contract = document.get("image_contract")
    if type(contract) is not dict or set(contract.keys()) != CONTRACT_FIELDS:
        raise FilmError("image_contract must hold exactly the contracted fields")
    _int(contract.get("width"), "image_contract.width", 1)
    _int(contract.get("height"), "image_contract.height", 1)
    if type(contract.get("pixel_format")) is not str \
            or not contract["pixel_format"]:
        raise FilmError("image_contract.pixel_format must be a non-empty string")
    if type(contract.get("alpha")) is not bool:
        raise FilmError("image_contract.alpha must be a boolean")
    if type(contract.get("color")) is not str or not contract["color"]:
        raise FilmError("image_contract.color must be a non-empty string")
    _int(contract.get("max_encoded_bytes"), "max_encoded_bytes", 1)
    _int(contract.get("max_decoded_bytes"), "max_decoded_bytes", 1)
    members = document.get("members")
    if type(members) is not list or not members \
            or document.get("member_count") != len(members):
        raise FilmError("members must be a non-empty list matching member_count")
    coverage = document.get("frame_coverage")
    if (type(coverage) is not list or len(coverage) != 2
            or any(type(v) is not int or v < 0 for v in coverage)
            or coverage[0] >= coverage[1]):
        raise FilmError("frame_coverage must be a [start, end) integer range")
    cursor = HEADER_BYTE_LENGTH
    member_ids, frame_indices = set(), set()
    for position, member in enumerate(members):
        if type(member) is not dict or set(member.keys()) - MEMBER_FIELDS:
            raise FilmError(f"Malformed pack member entry at position {position}")
        if type(member.get("member_id")) is not str or not member["member_id"]:
            raise FilmError("member_id must be a non-empty string")
        if member["member_id"] in member_ids:
            raise FilmError(f"Duplicate member id: {member['member_id']}")
        member_ids.add(member["member_id"])
        frame_index = member.get("frame_index")
        if frame_index is not None:
            _int(frame_index, "member frame_index")
            if frame_index in frame_indices:
                raise FilmError(f"Duplicate delivery frame_index: {frame_index}")
            frame_indices.add(frame_index)
            if not coverage[0] <= frame_index < coverage[1]:
                raise FilmError("Member frame_index outside frame_coverage")
        elif type(member.get("source_role")) is not str \
                or not member.get("source_role"):
            raise FilmError("A member needs a frame_index or a source_role")
        offset = _int(member.get("byte_offset"), "member byte_offset")
        length = _int(member.get("byte_length"), "member byte_length", 1)
        # No header intrusion, overlap, gap or reorder: offsets are the
        # contiguous index order starting right after the 16-byte header.
        if offset != cursor:
            raise FilmError("Pack member ranges must be contiguous in index "
                            "order starting at the header boundary")
        if offset + length > pack_length:
            raise FilmError("Member range exceeds pack_byte_length")
        if length > document["image_contract"]["max_encoded_bytes"]:
            raise FilmError("Member byte_length exceeds max_encoded_bytes")
        _sha(member.get("sha256"), "member sha256")
        cursor = offset + length
    if cursor != pack_length:
        raise FilmError("Pack members do not cover pack_byte_length")
    if frame_indices != set(range(coverage[0], coverage[1])):
        raise FilmError("frame_coverage must be covered without gaps")
    if type(document.get("retention_refs")) is not list:
        raise FilmError("retention_refs must be a list")
    return document


def index_bytes(document):
    return canon_bytes(document)


def read_index(raw, expected_sha256=None):
    """Parse a stored index object; the pinned index hash is checked first."""
    if len(raw) > INDEX_MAX_BYTES:
        raise FilmError("pack_index exceeds the 16 MiB read bound; refusing "
                        "member decode")
    if expected_sha256 is not None and sha256_bytes(raw) != expected_sha256:
        raise FilmError("pack_index hash does not match the archive pin")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FilmError("pack_index is not canonical JSON") from e
    if raw != canon_bytes(document):
        raise FilmError("pack_index is not stored in canonical form")
    return validate_index(document)


def read_index_file(path, expected_sha256=None):
    return read_index(Path(path).read_bytes(), expected_sha256)


def frame_members(index):
    """Members ordered by frame_index; halo/source-role members are separate."""
    frames = sorted((m for m in index["members"]
                     if m.get("frame_index") is not None),
                    key=lambda m: m["frame_index"])
    source = [m for m in index["members"] if m.get("frame_index") is None]
    return frames, source


def member_for_frame(index, frame_index):
    for member in index["members"]:
        if member.get("frame_index") == frame_index:
            return member
    raise FilmError(f"frame_index {frame_index} is not a pack member")
