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
from engine.core import read
from engine.core import production_profile

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
        # Display only: with no fresh observation the stored claim is
        # shown (trust_stored) and labelled as such — the execution
        # gate never reads this view and re-probes instead.
        status = capability_registry.registry_status(
            state_dir or settings.home(), trust_stored=True)
    except Exception as exc:
        st.error(f"capability registry를 읽지 못했습니다: {exc}")
        return
    if not status["entries"]:
        st.info("등록된 capability evidence가 없습니다. probe가 없으면 어떤 "
                "candidate도 QUALIFIED_FOR_SCOPE가 아닙니다.")
    st.caption("현재 observation 없이 저장된 claim 기준 표시 "
               "(information only) — 실행 gate는 이 표시를 신뢰하지 않고 "
               "재-probe가 필요합니다.")
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


# --- film-resource-forecast ---------------------------------------------------

def _lv(line):
    """A forecast line's display value (UNKNOWN stays literal)."""
    return line["value"] if type(line) is dict else line


def render_forecast(project=None, state_dir=None):
    """Read-only pre-execution resource forecast panel.

    Shows the ExecutionPlan preview's per-candidate resource breakdown —
    every number labelled with the measurement it came from or UNKNOWN —
    plus refusal reasons and proposed alternatives. The panel never
    executes, submits or charges anything.
    """
    st.divider()
    st.subheader("실행 전 자원 예측 · resource forecast")
    st.caption("작업 시작 전 예상 CPU·메모리·디스크·전송·구독 사용량과 "
               "불확실성을 봅니다 — 어떤 것도 실행하거나 청구하지 않습니다.")
    p = Path(project) if project else None
    if p is None:
        st.info("프로젝트를 선택하면 자원 예측을 표시합니다.")
        return
    try:
        if production_profile(read(p / "project.yaml")) \
                != "FRAME_ANIMATION_V1":
            st.caption("자원 예측은 FRAME_ANIMATION_V1 실행 계획이 있는 "
                       "프로젝트에서 사용할 수 있습니다.")
            return
        from engine.resource_forecast import resource_forecast_command
        forecast = resource_forecast_command(p, state_dir=state_dir)
    except Exception as exc:
        st.error(f"자원 예측을 만들지 못했습니다: {exc}")
        return
    st.caption(f"계획 `{forecast['plan']['plan_sha'][:12]}…` · 정책 "
               f"{forecast['plan']['policy']} · route "
               f"{forecast['plan']['transfer_route']} · "
               "미리보기 전용 — 실행되지 않습니다")
    rows = []
    for r in forecast["operations"]:
        rows.append({
            "candidate": r["candidate_id"],
            "route": r["route"] or r["driver"] or "-",
            "state": r["registry_state"],
            "선택가능": "O" if r["selectable"] else "X",
            "cold_ms": _lv(r["time_ms"]["cold"]),
            "warm_ms": _lv(r["time_ms"]["warm"]),
            "RAM_B": _lv(r["peaks"]["ram_bytes"]),
            "disk_B": _lv(r["peaks"]["disk_bytes"]),
            "VRAM_B": _lv(r["peaks"]["vram_bytes"]),
            "읽기_B": _lv(r["transfer"]["read_bytes"]),
            "쓰기_B": _lv(r["transfer"]["write_bytes"]),
            "요청": _lv(r["transfer"]["requests"]),
            "출처": r["time_ms"]["cold"]["basis"]})
    st.dataframe(rows, use_container_width=True, hide_index=True)
    for r in forecast["operations"]:
        provenance = (r["time_ms"]["cold"].get("source")
                      or r["peaks"]["ram_bytes"].get("source"))
        if provenance or r["reasons"]:
            with st.expander(f"{r['candidate_id']} — 측정 출처 · 사유",
                             expanded=False):
                st.json({"registry_state": r["registry_state"],
                         "source": provenance or "없음 (UNKNOWN)",
                         "requested_scope": r["requested_scope"],
                         "reasons": r["reasons"]})
    totals = forecast["totals"]
    st.caption(
        "합계 — cold {c} ms · 전송 {rb} B 읽기 / {wb} B 쓰기 / {rq} 요청 · "
        "출력 {ob} B".format(
            c=_lv(totals["cpu_ms"]), rb=_lv(totals["transfer"]["read_bytes"]),
            wb=_lv(totals["transfer"]["write_bytes"]),
            rq=_lv(totals["transfer"]["requests"]),
            ob=_lv(totals["output"]["encoded_bytes"])))
    for check in forecast["workspace"]["reservations"]:
        if check["sufficient"] is False:
            st.warning(f"디스크 부족 — {check['location']}: "
                       f"필요 {_lv(check['needed_bytes'])} B, "
                       f"여유 {_lv(check['free_bytes'])} B")
    for service, sub in forecast["subscription"].items():
        verdict = sub["charge"]["verdict"]
        basis = (sub.get("cost_estimate") or {}).get("basis") or "UNKNOWN"
        line = (f"**구독** `{service}` — 포함: "
                f"**{sub['inclusion']}** · 청구 판정: "
                f"**{verdict}** · 비용 근거: **{basis}** · quote: "
                f"**{sub['quote']['status']}**")
        if verdict == "USAGE_UNKNOWN":
            st.warning(line)
        else:
            st.markdown(line)
        if sub["charge"].get("reason"):
            st.caption(sub["charge"]["reason"])
    for refusal in forecast["refusals"]:
        st.warning(f"거부 사유 {refusal['code']}: {refusal['reason']}")
    for alt in forecast["alternatives"]:
        st.info(f"대안 제안 {alt['kind']}: {alt['reason']} — "
                "제안일 뿐 자동 실행되지 않습니다")
    facets = forecast["facets"]
    st.caption("qualification: **{q}** · acceptance: **{a}** · release: "
               "**{r}** — 예측은 실행·승인·qualification이 아닙니다".format(
                   q=facets["qualification_state"],
                   a=facets["acceptance_state"],
                   r=facets["release_state"]))
