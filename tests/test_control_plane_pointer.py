import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = json.loads((ROOT / ".github/control-plane-client.json").read_text(encoding="utf-8"))
POINTER = (ROOT / "docs/CONTROL_PLANE_POINTER.md").read_text(encoding="utf-8")
STUBS = ["RUNBOOKS/BOUNDARY.md", "RUNBOOKS/DISPATCH.md", "docs/CONTROL_PLANE_FLOW.md",
         "docs/CONTROL_PLANE_RUNTIME.md", "docs/KIX_CONTROL_PLANE_POINTER.md"]


def test_pointer_link_matches_machine_readable_pin():
    links = re.findall(r"^Pinned policy: https://github\.com/(\S+)/tree/([0-9a-f]{40})/(\S+)$", POINTER, re.M)
    assert links == [(CLIENT["control_repository"], CLIENT["control_source_commit"],
                      CLIENT["control_source_subdirectory"])]


def test_pointer_status_matches_machine_readable_status():
    assert re.findall(r"^Status: `([A-Z_]+)`$", POINTER, re.M) == [CLIENT["status"]]


def test_compatibility_stubs_redirect_without_pin_or_status():
    for path in STUBS:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "CONTROL_PLANE_POINTER.md" in text, path
        assert not re.search(r"\b[0-9a-f]{40}\b", text), path
        assert CLIENT["status"] not in text, path
