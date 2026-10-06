"""ANIM-019 read-only capability registry panel.

Lists every stored capability evidence entry with its *current* computed
state — DOCUMENTED_ONLY / QUALIFIED_FOR_SCOPE / STALE / UNAVAILABLE —
and a human-readable reason; digests and bindings stay in an expander.
The panel is read-only: it never probes, writes or upgrades a record, and
it keeps the honest boundary that a fake/fixture PASS is never real
qualification — the facets stay UNQUALIFIED / PENDING / NOT_AUTHORIZED.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine import capability_registry, settings

_STATE_TEXT = {
    "QUALIFIED_FOR_SCOPE": "실제 probe가 이 scope를 확인했습니다 — 그 scope 안에서만 유효합니다.",
    "STALE": "환경·사용권이 바뀌었거나 만료됐습니다 — 이전 PASS는 연장되지 않으며 재-probe가 필요합니다.",
    "UNAVAILABLE": "실제 probe 결과 이 capability는 없습니다 — 없는 자원으로 표시되지 않습니다.",
    "DOCUMENTED_ONLY": "문서/선언 또는 fake fixture만 있습니다 — 실제 scope 확인이 없어 qualification이 아닙니다.",
}


def _reason_text(entry):
    base = _STATE_TEXT.get(entry["state"], entry["state"])
    reasons = ", ".join(entry["reasons"])
    detail = entry.get("detail") or ""
    parts = [base]
    if reasons:
        parts.append(f"사유: {reasons}")
    if detail:
        parts.append(str(detail))
    return " · ".join(parts)


def render_capabilities(project=None, state_dir=None):
    """The capability registry section for the connection tab."""
    st.divider()
    st.subheader("실행 자격 registry · capability 상태")
    st.caption("scope-묶인 evidence만 표시합니다 — fake/fixture PASS는 "
               "qualification이 아니며, 환경·사용권 변경이나 만료는 STALE입니다.")
    try:
        status = capability_registry.registry_status(
            state_dir or settings.home())
    except Exception as exc:
        st.error(f"capability registry를 읽지 못했습니다: {exc}")
        return
    if not status["entries"]:
        st.info("등록된 capability evidence가 없습니다. probe가 없으면 어떤 "
                "candidate도 QUALIFIED_FOR_SCOPE가 아닙니다.")
    for entry in status["entries"]:
        fake = " · FAKE" if entry["fake"] else ""
        st.markdown(f"**{entry['axis']}** · `{entry['evidence_id']}` — "
                    f"**{entry['state']}**{fake}")
        st.caption(f"{entry['driver']} · {entry['operation']} · "
                   f"{_reason_text(entry)}")
        with st.expander("해시 · binding", expanded=False):
            st.json({"evidence_id": entry["evidence_id"],
                     "adapter_digest": entry["adapter_digest"],
                     "account_binding": entry["account_binding"],
                     "credential_epoch": entry["credential_epoch"],
                     "route": entry["route"],
                     "transport": entry["transport"],
                     "scope": entry["scope"],
                     "fixture": entry["fixture"],
                     "observed_at": entry["observed_at"]})
    for record in status["measurements"]:
        missing = record["missing"]
        state = "UNKNOWN (부분 측정)" if missing else "측정 완료"
        st.caption(f"측정 `{record['measurement_id']}` → "
                   f"{record['evidence_id']}: {state}"
                   + (f" · 누락 {', '.join(missing)}" if missing else ""))
    facets = status["facets"]
    st.caption("qualification: **{q}** · acceptance: **{a}** · release: "
               "**{r}** — fixture/fake 성공은 실제 자격이 아닙니다".format(
                   q=facets["qualification_state"],
                   a=facets["acceptance_state"],
                   r=facets["release_state"]))
