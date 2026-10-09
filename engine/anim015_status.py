"""ANIM-015 acceptance report.

The 2026-10-08 chat note ("FFmpeg 넣어") is recorded as WAIVED in the
encoding-decision evidence. That note did not revise the pinned plan or
ADR 0001 §10/§11, and there is no User decision in docs/decisions that
ratifies it. This module reports the node as merged with that acceptance
item still open. It does not encode, probe hardware, or qualify a driver.
"""
from pathlib import Path
import json

from .animation_schema import read_canon

PINNED_ACCEPTANCE = "NO_FFMPEG_ENCODING 경로 실제 성공"
ADR_NATIVE_SENTENCE = (
    "native encode가 실제로 같은 DeliveryProfile의 프레임, PTS, 원곡을 "
    "만들기 전에는 복수 driver 완료로 세지 않는다")
RATIFICATION_MARKER = "ANIM015-PLAN-RATIFIED"


def _repo_root():
    return Path(__file__).resolve().parents[1]


def anim015_acceptance_status(root=None):
    """Report anim-015 as merged with NO_FFMPEG_ENCODING still open.

    A recorded chat-note waiver does not close the pinned acceptance.
    Qualification stays UNQUALIFIED and release stays NOT_AUTHORIZED.
    """
    root = Path(root) if root is not None else _repo_root()
    decision = read_canon(
        root / "docs" / "evidence" / "anim-015" / "encoding-decision.json")
    program = json.loads(
        (root / ".aiops" / "program.json").read_text(encoding="utf-8"))
    node = next(item for item in program["nodes"]
                if item.get("id") == "anim-015")
    contract = (root / "docs" / "adr"
                / "0001-frame-animation-v1-contract.md").read_text(
                    encoding="utf-8")
    decision_dir = root / "docs" / "decisions"
    ratified = any(
        RATIFICATION_MARKER in path.read_text(encoding="utf-8")
        for path in sorted(decision_dir.glob("*.md")))
    facet = decision["facets"]["NO_FFMPEG_ENCODING"]
    demonstrated = "NOT_DEMONSTRATED" in json.dumps(
        facet, ensure_ascii=False)
    pinned = PINNED_ACCEPTANCE in node["spec"]
    adr_open = ADR_NATIVE_SENTENCE in contract
    chat_waived = decision.get("requirement_state") == "WAIVED"
    # The chat note closes nothing unless a User decision ratifies a plan
    # revision. This checkout has no such record.
    closes = bool(ratified)
    return {
        "node": "anim-015",
        "node_report": "MERGED_WITH_OPEN_ACCEPTANCE",
        "acceptance_item": "NO_FFMPEG_ENCODING",
        "acceptance_state": "CLOSED" if closes else "OPEN",
        "demonstration": "NOT_DEMONSTRATED" if demonstrated
        else "UNSTATED",
        "chat_note_recorded": chat_waived,
        "chat_note_closes_acceptance": closes,
        "plan_ratified": ratified,
        "pinned_acceptance_present": pinned,
        "adr_requires_real_native_encode": adr_open,
        "qualification_state": "UNQUALIFIED",
        "release_state": "NOT_AUTHORIZED",
    }
