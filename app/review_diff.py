"""film-review-diff — 검수 지적과 수정 대조 ("12 · 검수 대조" tab).

Rendered only for FRAME_ANIMATION_V1 projects. The panel shows each
finding's cut, frame range, problem type and artifact hash, then the
before/after digests and the ANIM-020 impact closure. Resolving the
cited finding and needing a full-film playback are separate lines, and
author self-check, independent review and director approval stay
separate roles.

Refusal actions on the same screen attempt to carry an old PASS onto
the new render and to close a finding with another cut's revision;
both are engine refusals. Nothing here writes lyrics, cues, audio or
manifest locks. Qualification stays UNQUALIFIED, acceptance PENDING,
release NOT_AUTHORIZED.

AppTest does not observe the spinner frame or real keyboard focus
order. Those stay unverified in this environment. Numeric frame fields
are `st.number_input` (arrow keys) and the refusal paths are ordinary
buttons.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError
from engine.review_diff import (CUT_METHOD, DECISIONS, DISPOSITIONS,
                                PROBLEM_TYPES, ROLES, acknowledge,
                                carry_pass, diff_view, open_finding,
                                record_revision)

_ROLE_KO = {"AUTHOR_SELF_CHECK": "작성자 자기 확인",
            "INDEPENDENT_REVIEW": "독립 검토",
            "DIRECTOR_APPROVAL": "감독 승인"}
_DECISION_KO = {"CHECKED": "자기 확인만 기록",
                "FINDING_RESOLVED": "이 지적만 해소",
                "CUT_PASS": "컷 재생 PASS"}
_PROBLEM_KO = {"CONTACT": "접촉", "OCCLUSION": "가림", "ENDPOINT": "끝점",
               "FLICKER": "깜빡임", "MOTION": "동작", "APPEARANCE": "외형",
               "SUBTITLE": "자막", "TECHNICAL": "기술 실패"}
_DISPOSITION_KO = {"FIX_REQUIRED": "수정 필요", "INTENTIONAL": "의도된 표현",
                   "ACCEPTED_LIMITATION": "수용된 제한"}


def _guarded(fn):
    try:
        return fn()
    except FilmError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"처리하지 못했습니다: {exc}")
    return None


def _range_text(span):
    return f"[{span[0]}, {span[1]})"


def render_review_diff(p):
    """The finding / revision comparison tab."""
    p = Path(p)
    st.caption("지적한 문제를 그 컷·프레임·채택 시퀀스 해시에 묶고, 수정 "
               "전후와 영향 closure를 보여 줍니다. 작성자 자기 확인은 독립 "
               "검토나 감독 승인이 아니고, 옛 렌더의 PASS는 새 렌더로 "
               "승계되지 않습니다. 이 화면의 기록은 최종 승인이 아닙니다 — "
               "qualification UNQUALIFIED · acceptance PENDING · "
               "release NOT_AUTHORIZED.")
    flash = st.session_state.pop("rd_flash", None)
    if flash:
        st.success(flash)
    view = _guarded(lambda: diff_view(p))
    if view is None:
        return
    waiting = view["screen_status"] == "empty"
    cols = st.columns(4)
    cols[0].metric("화면", "대기" if waiting else "대조 완료")
    cols[1].metric("지적", str(len(view["findings"])))
    cols[2].metric("해소", str(sum(
        1 for row in view["findings"] if row.get("finding_resolved"))))
    cols[3].metric("전체 재생",
                   "필요" if view["full_playback_required"] else "없음")
    st.caption(f"{view['fps']}fps · 출력 {view['output_frames']}프레임 · "
               "PASS 승계: 아니오 · 최종 승인 아님")
    if waiting:
        st.info("기록이 없습니다. 아래에서 컷, 프레임 범위, 문제 유형을 "
                "현재 채택 시퀀스 해시에 묶습니다.")
    else:
        st.dataframe([{
            "지적": row["finding_id"],
            "문제": row["finding"]["problem_type"],
            "컷": row["finding"]["instance_id"],
            "샷": row["finding"]["shot_id"],
            "프레임": _range_text(row["finding"]["frame_range"]),
            "해시": row["finding"]["artifact_sha256"][:12],
            "상태": row.get("finding_state") or row.get("error") or "-",
        } for row in view["findings"]], hide_index=True)
        by_id = {row["finding_id"]: row for row in view["findings"]}
        picked = st.selectbox(
            "대조할 지적", list(by_id), key="rd_finding",
            format_func=lambda fid: (
                f"{fid} · {by_id[fid]['finding']['problem_type']} · "
                f"{by_id[fid]['finding']['instance_id']}"))
        _detail(p, view, by_id[picked])
    _open_form(p, view)


def _detail(p, view, item):
    finding = item["finding"]
    st.markdown(
        f"**{finding['finding_id']} · {finding['problem_type']}"
        f"({_PROBLEM_KO.get(finding['problem_type'], '')}) · "
        f"{finding['instance_id']} {finding['shot_id']} · "
        f"프레임 {_range_text(finding['frame_range'])}**")
    st.caption(f"묶인 해시 {finding['artifact_sha256']} · "
               f"리비전 r{finding['sequence_revision']} · "
               f"자산 {finding['asset_id']} · 역할 "
               f"{_ROLE_KO.get(finding['role'], finding['role'])}")
    if item.get("error"):
        st.error(item["error"])
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("지적 해소", "예" if item["finding_resolved"] else "아니오")
    c2.metric("전체 재생",
              "필요" if item["full_playback_required"] else "없음")
    c3.metric("컷 PASS",
              "현재 해시" if item["cut_pass_covers_current"] else "승계 안 됨")
    st.caption("수정 전 " + item["before"]["artifact_sha256"])
    st.caption("수정 후 " + item["after"]["artifact_sha256"])
    changed = item["changed_member_indices"]
    st.caption("바뀐 프레임 " + (", ".join(str(i) for i in changed) or "없음")
               + (" · 지적 프레임이 바뀜"
                  if item["finding_frames_changed"] else
                  " · 지적 프레임은 그대로"))
    st.caption(item["full_playback_reason"])
    st.markdown(
        "작성자 자기 확인: "
        + ("기록됨" if item["author_self_check"] else "아님")
        + " · 독립 검토: "
        + ("기록됨" if item["independent_review"] else "아님")
        + " · 감독 승인: "
        + ("기록됨" if item["director_approval"] else "아님"))
    with st.expander("영향 closure", expanded=True):
        st.dataframe(
            [{"범위": name, "판정": "STALE"} for name in item["stale"]]
            + [{"범위": name, "판정": "KEPT"} for name in item["kept"]],
            hide_index=True)
        st.caption("영향 컷 " + ", ".join(item["closure"]["affected_instances"]
                                         or ["없음"])
                   + " · 다른 컷 검토는 KEPT로 남습니다.")
    _respond(p, view, item)


def _respond(p, view, item):
    finding = item["finding"]
    st.markdown("**수정 대조 기록**")
    rev_role = st.selectbox(
        "대조를 남기는 역할", ROLES, key="rd_rev_role",
        format_func=lambda role: _ROLE_KO[role])
    rev_reviewer = st.text_input("대조 작성자", key="rd_rev_reviewer")
    rev_note = st.text_input("수정 메모", key="rd_rev_note")
    if st.button("수정 대조 기록", key="rd_record"):
        with st.spinner("진행 중 — 수정 전후와 영향 closure를 계산하고 있습니다…"):
            saved = _guarded(lambda: record_revision(
                p, finding["finding_id"], reviewer=rev_reviewer,
                role=rev_role, note=rev_note))
        if saved is not None:
            st.session_state["rd_flash"] = (
                f"{saved['revision_id']} 대조를 기록했습니다 — "
                "PASS는 승계되지 않았습니다")
            st.rerun()
    st.markdown("**이 렌더에 대한 확인**")
    ack_role = st.selectbox(
        "확인 역할", ROLES, key="rd_ack_role",
        format_func=lambda role: _ROLE_KO[role])
    ack_decision = st.selectbox(
        "확인 종류", DECISIONS, key="rd_ack_decision",
        format_func=lambda decision: _DECISION_KO[decision])
    ack_hash = st.text_input(
        "대상 artifact hash", value=item["after"]["artifact_sha256"],
        key=f"rd_ack_hash_{finding['finding_id']}_{item['after']['artifact_sha256'][:8]}")
    ack_reviewer = st.text_input("확인자", key="rd_ack_reviewer")
    ack_note = st.text_input("확인 메모", key="rd_ack_note")
    ack_method = st.checkbox(
        "이 컷 전체를 정상속도로 재생해 봤습니다", key="rd_ack_method")
    if st.button("확인 기록", key="rd_ack"):
        methods = [CUT_METHOD] if ack_method else []
        with st.spinner("진행 중 — 현재 해시와 역할을 확인하고 있습니다…"):
            saved = _guarded(lambda: acknowledge(
                p, finding["finding_id"], role=ack_role,
                reviewer=ack_reviewer, decision=ack_decision,
                artifact_sha256=ack_hash.strip(), note=ack_note,
                methods=methods))
        if saved is not None:
            st.session_state["rd_flash"] = (
                f"{saved['ack_id']} {_ROLE_KO[saved['role']]} · "
                f"{_DECISION_KO[saved['decision']]}")
            st.rerun()
    st.markdown("**거절 시나리오**")
    if st.button("옛 PASS 승계 시도", key="rd_carry"):
        def refuse_stale():
            if item["changed"]:
                return acknowledge(
                    p, finding["finding_id"], role="DIRECTOR_APPROVAL",
                    reviewer="거절 시나리오", decision="CUT_PASS",
                    artifact_sha256=item["before"]["artifact_sha256"],
                    note="옛 렌더 해시로 PASS를 시도",
                    methods=[CUT_METHOD])
            return carry_pass(p, finding["instance_id"])
        _guarded(refuse_stale)
    if st.button("다른 컷 리비전으로 닫기", key="rd_other"):
        def refuse_other():
            others = [entry for entry in view["entries"]
                      if entry.get("resolved")
                      and entry["instance_id"] != finding["instance_id"]]
            if not others:
                raise FilmError("비교할 다른 컷이 없습니다")
            return acknowledge(
                p, finding["finding_id"], role="INDEPENDENT_REVIEW",
                reviewer="거절 시나리오", decision="FINDING_RESOLVED",
                artifact_sha256=item["after"]["artifact_sha256"],
                note="다른 컷 리비전으로 닫기 시도",
                instance_id=others[0]["instance_id"])
        _guarded(refuse_other)


def _open_form(p, view):
    st.divider()
    st.markdown("**새 지적**")
    resolved = [entry for entry in view["entries"] if entry.get("resolved")]
    if not resolved:
        st.info("채택된 시퀀스가 없습니다. 프레임을 가져온 뒤에 지적을 "
                "묶을 수 있습니다.")
        return
    meta = {entry["instance_id"]: entry for entry in resolved}
    instance = st.selectbox(
        "컷", list(meta), key="rd_open_instance",
        format_func=lambda iid: f"{iid} · {meta[iid]['shot_id']}")
    count = meta[instance]["frame_count"]
    start = st.number_input(
        "시작 프레임", min_value=0, max_value=max(0, count - 1),
        value=0, step=1, key="rd_start",
        help="키보드 화살표로 한 프레임씩 이동합니다. 0부터 사용하는 구간입니다.")
    end = st.number_input(
        "끝 프레임 (미포함)", min_value=1, max_value=max(1, count),
        value=1, step=1, key="rd_end",
        help="키보드 화살표로 끝 프레임을 바꿉니다. 끝은 포함하지 않습니다.")
    problem = st.selectbox(
        "문제 유형", PROBLEM_TYPES, key="rd_problem",
        format_func=lambda kind: f"{kind} · {_PROBLEM_KO[kind]}")
    disposition = st.selectbox(
        "처분", DISPOSITIONS, key="rd_disposition",
        format_func=lambda kind: f"{kind} · {_DISPOSITION_KO[kind]}")
    role = st.selectbox(
        "지적한 역할", ROLES, index=1, key="rd_open_role",
        format_func=lambda kind: _ROLE_KO[kind])
    reviewer = st.text_input("검토자", key="rd_open_reviewer")
    note = st.text_input("지적 내용", key="rd_open_note")
    reason = st.text_input("면제 이유 (의도된 표현·수용된 제한만)", key="rd_open_reason")
    if st.button("지적 기록", key="rd_open"):
        with st.spinner("진행 중 — 컷·프레임·해시에 지적을 묶고 있습니다…"):
            saved = _guarded(lambda: open_finding(
                p, instance, [int(start), int(end)], problem,
                reviewer=reviewer, role=role, note=note,
                disposition=disposition, reason=reason.strip() or None))
        if saved is not None:
            st.session_state["rd_flash"] = (
                f"{saved['finding_id']}를 {saved['shot_id']} "
                f"{_range_text(saved['frame_range'])}에 묶었습니다")
            st.rerun()
