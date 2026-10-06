"""Shared synthetic fixtures for ANIM-013 tests.

Small Pillow PNGs and member dicts; every Drive interaction goes through
the explicit fake backend — no network, no credentials, no real OAuth.
"""
import io

from PIL import Image

from engine.drive_oauth import (FakeOAuthFlow, MemoryTokenStore,
                                connect_drive)
from engine.storage_backends.fake_drive import FakeDriveBackend


def make_png(seed=0, width=24, height=24, alpha=255):
    """Deterministic small PNG; each seed yields distinct bytes."""
    image = Image.new("RGBA", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7 + seed * 31) % 256,
                            (y * 5 + seed * 17) % 256,
                            (x * y + seed * 11) % 256, alpha)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def make_members(count=4, seed=0, width=24, height=24):
    """Member dicts as archive_frames/build_pack consume them."""
    return [{"member_id": f"F{index:03d}", "frame_index": index,
             "data": make_png(seed + index, width, height)}
            for index in range(count)]


def fake_backend(provider_checksum="sha256", **kwargs):
    return FakeDriveBackend(provider_checksum=provider_checksum, **kwargs)


def connect(backend=None, **flow_kwargs):
    """A DriveSession over the fake flow + memory token store."""
    backend = backend or fake_backend()
    return connect_drive(FakeOAuthFlow(**flow_kwargs), MemoryTokenStore(),
                         backend)


def archived(members=None, backend=None, **kwargs):
    """One sealed archive on a fake backend; returns (result, backend)."""
    from engine.drive_archive import archive_frames
    members = members or make_members()
    backend = backend or fake_backend()
    result = archive_frames(backend, members, profile="DRIVE_BOUNDED",
                            **kwargs)
    return result, backend
