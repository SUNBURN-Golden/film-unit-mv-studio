"""film-quality-diagnostics results panel.

Renders the technical diagnostics report of one sealed build inside the
control panel. The panel only surfaces engine/quality_diagnostics.py
measurements — decode/PTS/duration/audio-channel/clipping/silence/font
checks plus black-frame and still runs — and drives the finding
classification record. It is technical assistance only: the report carries
`not_an_approval`, no aesthetic score exists anywhere in the pipeline, and
nothing here touches the review/LOCK binding or the artifact bytes.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError
from engine.quality_diagnostics import (CLASSIFICATIONS, classify_finding,
                                        diagnose_build, load_report)

DECISION_KO = {"ACCEPTED_INTENDED": "의도된 표현으로 수용",
               "DEFECT": "결함으로 확정",
               "NOT_A_DEFECT": "결함 아님"}
SEVERITY_BADGE = {"DEFECT": "🔴 DEFECT", "CANDIDATE": "🟡 CANDIDATE",
                  "INTENDED": "🟢 INTENDED", "INFO": "ℹ️ INFO"}


def _guarded(fn):
    try:
        return fn()
    except (FilmError, ValueError, OSError) as exc:
        st.error(str(exc))
        return None


def _loc_text(location):
    if location.get("start_frame") is not None:
        return (f"프레임 {location['start_frame']}–{location['end_frame']} "
                f"({location['start_ms']}–{location['end_ms']} ms)")
    if location.get("start_ms") is not None:
        return f"{location['start_ms']}–{location['end_ms']} ms"
    return location.get("stream") or "-"


def _prov_text(prov):
    if not prov:
        return ""
    bits = [f"build {prov.get('build_id')}" if prov.get("build_id") else ""]
    if prov.get("encode_digest"):
        bits.append(f"encode {prov['encode_digest'][:12]}…")
    if prov.get("artifact_sha256_recorded"):
        bits.append(f"기록 해시 {prov['artifact_sha256_recorded'][:12]}…")
    return " · ".join(b for b in bits if b)


def render_diagnostics(p, build_id):
    """The technical diagnostics section for the selected sealed build."""
    p = Path(p)
    st.markdown("**기술 진단 — decode·시간축·오디오·자막**")
    st.caption("자동 기술 측정(not_an_approval)입니다. Final 승인도, 미학 "
               "점수도 아니며 검수·LOCK·승인 상태와 원본 바이트를 바꾸지 "
               "않습니다. 후보 finding은 사람이 근거와 함께 분류합니다.")
    intended_text = st.text_input(
        "의도 선언 추가 (KIND:시작ms-끝ms, … — BLACK/STILL/SILENCE)",
        key=f"qd_intended_{build_id}",
        help="계획·타임라인에 선언된 의도된 정지·암전·무음은 자동으로 "
             "INTENDED로 표시됩니다. 여기서 추가 선언할 수 있습니다.")
    if st.button("진단 실행", key=f"qd_run_{build_id}"):
        from engine.quality_diagnostics import parse_intended

        def run_diag():
            declared = parse_intended(intended_text) \
                if intended_text.strip() else None
            return diagnose_build(p, build_id, intended=declared)
        with st.spinner("전체 디코드·패킷·PCM을 측정하고 있습니다…"):
            report = _guarded(run_diag)
        if report is not None:
            st.session_state[f"qd_done_{build_id}"] = report["summary"]
            st.rerun()
    report = _guarded(lambda: load_report(p, build_id))
    done = st.session_state.pop(f"qd_done_{build_id}", None)
    flash = st.session_state.pop(f"qd_flash_{build_id}", None)
    if flash:
        st.success(flash)
    if done is not None:
        st.success(f"진단 완료 — DEFECT {done['DEFECT']} · "
                   f"CANDIDATE {done['CANDIDATE']} · "
                   f"INTENDED {done['INTENDED']}")
    if report is None:
        st.caption("이 빌드의 진단 결과가 아직 없습니다. 위 버튼으로 "
                   "측정합니다.")
        return
    summary = report["summary"]
    cols = st.columns(4)
    cols[0].metric("DEFECT", summary["DEFECT"])
    cols[1].metric("CANDIDATE", summary["CANDIDATE"])
    cols[2].metric("INTENDED", summary["INTENDED"])
    cols[3].metric("미분류 후보", summary["unclassified_candidates"])
    st.caption(f"측정 {report['created_at']} · ffmpeg "
               f"{report['tool']['ffmpeg'].split()[2] if len(report['tool']['ffmpeg'].split()) > 2 else report['tool']['ffmpeg']} · "
               f"빌드 상태 {report.get('build_status') or '-'}")
    findings = report["findings"]
    if not findings:
        st.success("발견된 finding이 없습니다 — 측정된 기술 지표가 계약과 "
                   "일치합니다.")
    for finding in findings:
        badge = SEVERITY_BADGE.get(finding["severity"],
                                   finding["severity"])
        classified = finding.get("classification")
        state = (f" → {classified['decision']} "
                 f"({classified['reviewer']})" if classified else "")
        with st.expander(
                f"{finding['id']} {badge} {finding['kind']} · "
                f"{finding['artifact']} · {_loc_text(finding['location'])}"
                f"{state}", expanded=False):
            st.write(finding["detail"])
            st.caption("위치: " + _loc_text(finding["location"]))
            st.caption("재현 명령")
            st.code(finding["repro"], language="bash")
            prov = _prov_text(finding.get("provenance"))
            if prov:
                st.caption("provenance: " + prov)
            if classified:
                st.info(f"{classified['classified_at']} · "
                        f"{classified['reviewer']}: "
                        f"{classified['decision']} — "
                        f"{classified['reason']}")
    classifiable = [f for f in findings
                    if f["severity"] in ("CANDIDATE", "INTENDED")
                    and not f.get("classification")]
    defects = [f for f in findings if f["severity"] == "DEFECT"
               and not f.get("classification")]
    classifiable += defects
    if classifiable:
        st.markdown("**후보 분류 — 근거 필수, 원본은 바뀌지 않습니다**")
        labels = {f"{f['id']} · {f['severity']} · {f['kind']} · "
                  f"{_loc_text(f['location'])}": f["id"]
                  for f in classifiable}
        pick_label = st.selectbox("분류할 finding", list(labels),
                                  key=f"qd_pick_{build_id}")
        decision = st.radio(
            "분류", CLASSIFICATIONS,
            format_func=lambda d: f"{d} — {DECISION_KO[d]}",
            key=f"qd_decision_{build_id}")
        reason = st.text_input("근거 (필수)", key=f"qd_reason_{build_id}")
        reviewer = st.text_input("분류자 (필수)", key=f"qd_reviewer_{build_id}")
        if st.button("분류 기록", key=f"qd_classify_{build_id}",
                     disabled=not (reason.strip() and reviewer.strip())):
            result = _guarded(lambda: classify_finding(
                p, build_id, labels[pick_label], decision, reason,
                reviewer))
            if result is not None:
                st.session_state[f"qd_flash_{build_id}"] = (
                    f"{labels[pick_label]}를 {decision}로 기록했습니다 — "
                    "원본 파일은 그대로입니다")
                st.rerun()
        st.caption("DEFECT는 규격 위반이라 ACCEPTED_INTENDED로 면책할 수 "
                   "없습니다 — 고치거나 DEFECT로 확정합니다.")
    with st.expander("진단 보고서 원문 (JSON)", expanded=False):
        st.json(report)
