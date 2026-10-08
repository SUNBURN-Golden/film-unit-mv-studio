"""film-delivery-package panel inside the animation output section.

Shows Preview, Final-candidate, director-adoption and external-publication
as words, then writes a bundle only when the user asks. The panel does
not approve artwork, qualify a service or release anything. LEGACY_MV
never reaches this module: the animation tab is not mounted for it.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError
from engine.delivery_package import (assemble_bundle, delivery_status,
                                     verify_bundle)

_PREVIEW = {"PREVIEW": "미리보기", "NOT_PREVIEW": "미리보기 아님"}
_CANDIDATE = {"FINAL_CANDIDATE_READY": "Final 후보",
              "NOT_A_CANDIDATE": "Final 후보 아님"}
_ADOPTION = {"NOT_ADOPTED": "감독 미채택",
             "PROTOCOL_ADOPTED": "감독 프로토콜 채택 · 작품 수용 아님"}


def _bundle_dir(project, build_id):
    return Path(project) / "delivery" / build_id


def render_delivery(project, build_id):
    """Normal, empty, error and in-progress states for one build.

    ``build_id`` None is the empty state (no sealed build yet). Buttons
    are ordinary Streamlit controls, so keyboard focus follows the widget
    order. A real browser pass is not claimed by this module.
    """
    st.markdown("**전달 묶음**")
    st.caption("영상 파일과 그 파일이 무엇인지 증명하는 자료입니다. "
               "파일 이름에 Final이 있어도 승인·채택·공개가 되지 않습니다. "
               "qualification UNQUALIFIED · 작품 수용 PENDING · "
               "외부 공개 NOT_AUTHORIZED.")
    if not build_id:
        st.caption("전달 묶음: 봉인된 빌드가 없습니다.")
        return
    try:
        status = delivery_status(project, build_id)
    except FilmError as exc:
        st.error(str(exc))
        return
    states = status["states"]
    st.caption("미리보기: " + _PREVIEW.get(states["preview"], states["preview"]))
    st.caption("Final 후보: " + _CANDIDATE.get(
        states["final_candidate"], states["final_candidate"]))
    st.caption("감독 채택: " + _ADOPTION.get(
        states["director_adoption"], states["director_adoption"])
        + " · 작품 수용 PENDING")
    st.caption("외부 공개: NOT_AUTHORIZED")
    st.caption("전달 profile: "
               + (status["delivery_profile"] or "없음")
               + " · " + status["profile_check"]
               + " · 승인 범위 " + status["scope_match"])
    destination = _bundle_dir(project, build_id)
    if destination.is_dir():
        st.caption("진행: 묶음이 있습니다. 다시 만들려면 그 폴더를 옮기세요.")
    else:
        st.caption("진행: 대기 — 아래 버튼이 묶음을 만듭니다.")
    make = st.button("전달 묶음 만들기", key=f"dp_make_{build_id}")
    verify = st.button("묶음 무결성 확인", key=f"dp_verify_{build_id}")
    if make:
        with st.spinner("전달 묶음을 만들고 있습니다…"):
            try:
                result = assemble_bundle(project, build_id, destination)
            except FilmError as exc:
                st.error(str(exc))
            else:
                st.success(
                    f"{result['bundle_id']} 묶음을 만들었습니다. "
                    f"감독 채택 {result['states']['director_adoption']} · "
                    f"외부 공개 {result['states']['external_publication']} · "
                    f"작품 수용 PENDING")
    if verify:
        if not (destination / "bundle.json").is_file():
            st.error("확인할 전달 묶음이 없습니다.")
        else:
            with st.spinner("다운로드 해시를 다시 계산하고 있습니다…"):
                checked = verify_bundle(destination)
            if checked["valid"]:
                st.success("묶음 해시가 manifest와 같습니다.")
            else:
                st.error("무결성 실패: " + "; ".join(checked["errors"]))
