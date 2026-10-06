"""Real Google Drive backend — a deliberately disabled stub (ANIM-013).

No OAuth client registration exists for this program, and this code must
never hold a client secret or user token. Constructing the backend without
an issued client configuration raises GOOGLE_DRIVE_NOT_CONFIGURED; with a
configuration it still raises GOOGLE_DRIVE_UNQUALIFIED — the real route has
no qualification evidence, and nothing here may silently count as one.
"""
from ..core import FilmError


class GoogleDriveBackend:
    name = "GOOGLE_DRIVE"
    provider_checksum = None

    def __init__(self, client_config=None):
        if not client_config:
            raise FilmError(
                "GOOGLE_DRIVE_NOT_CONFIGURED: no OAuth client is issued for "
                "this program; issuing one is the deploy owner's task and "
                "the fake backend is used for protocol checks meanwhile")
        raise FilmError(
            "GOOGLE_DRIVE_UNQUALIFIED: no real Drive connectivity has been "
            "qualified; real Google access stays blocked until a probed "
            "backend exists")
