"""제작 작업실 ("13 · 작업실" tab).

Rendered only for FRAME_ANIMATION_V1. The screen says what to do next
(준비 → 동작 → 검토 → 수정 → 출력), moves a long timeline by number,
arrow-sized buttons and zoom, and keeps a typed note plus the saved
frame when a save is refused. Status uses a word and a mark together.

LEGACY_MV never mounts this tab. Nothing here edits lyrics, cues,
audio, reviews or locks, and nothing starts a login or a retry.
Qualification stays UNQUALIFIED, acceptance PENDING, release
NOT_AUTHORIZED.

AppTest does not press real keys or resize a desktop window. Those
checks stay unverified in this environment. Every control is a labelled
Streamlit widget so Tab / arrow keys / Enter are the journey.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError
from engine.workspace_accessibility import (
    ZOOM_ORDER, clamp_frame, commit_workspace, credential_view, default_prefs,
    journey, keyboard_targets, load_prefs, restart_view, timeline_focus,
    zoom_step)

_ZOOM_LABEL = {
    "wide": "넓게 · 480프레임",
    "span": "구간 · 96프레임",
    "second": "1초 · 24프레임",
    "close": "가깝게 · 8프레임",
    "frame": "한 프레임",
}

_REDUCED_CSS = """
<style>
@media (prefers-reduced-motion: reduce) {
  .stApp, .stApp * { animation: none !important; transition: none !important; }
}
@media (max-width: 880px) {
  .wa-help { display: block; width: 100%; }
  .st-key-wa_stages [data-testid="stHorizontalBlock"] {
    flex-direction: column !important;
  }
  .st-key-wa_stages [data-testid="stColumn"] {
    width: 100% !important;
    flex: 1 1 auto !important;
  }
}
.wa-reduced-on, .wa-reduced-on * {
  animation: none !important; transition: none !important;
}
</style>
"""

_REDUCED_ON_CSS = """
<style id="wa-reduced-app">
.stApp, .stApp * { animation: none !important; transition: none !important; }
</style>
"""


def _init(key, value):
    if key not in st.session_state:
        st.session_state[key] = value


def _nudge(move):
    """on_click: runs before widgets, so the frame field keeps the new index."""
    total = st.session_state.get("wa_total")
    if type(total) is not int or total < 1:
        return
    try:
        frame = clamp_frame(total, int(st.session_state.get("wa_frame", 0)))
        targets = keyboard_targets(total, frame, st.session_state["wa_zoom"])
    except (FilmError, TypeError, ValueError):
        return
    st.session_state["wa_frame"] = targets[move]


def _zoom(direction):
    st.session_state["wa_zoom"] = zoom_step(
        st.session_state["wa_zoom"], direction)


def _status_line(cred, restart):
    st.markdown(
        f"**자격** {cred['text']} · 연결 기록 {cred['connection_record']} · "
        f"**재시작** {restart['text']} · 자동 재시도: 아니오")
    st.caption(cred["detail"])
    st.caption(restart["detail"])
    st.caption("qualification UNQUALIFIED · acceptance PENDING · "
               "release NOT_AUTHORIZED. 미리보기와 이 화면은 완성본이 아닙니다.")


def _journey_block(guide, narrow):
    legend = " · ".join(item["text"] for item in guide["legend"])
    st.markdown(f"상태 글자: {legend}")
    st.caption("검수·실패·진행은 색만이 아니라 위의 글자와 기호로 구분합니다.")
    layout = "좁게" if narrow else "넓게"
    st.caption(f"화면 배치: {layout}")
    st.markdown(
        f'<div class="wa-help" data-layout="{layout}">작업실</div>',
        unsafe_allow_html=True)
    if guide["work_empty"]:
        st.info("아직 넣은 그림이 없습니다. 자산 화면에서 그림 묶음을 넣으면 "
                "이 안내가 다음 단계로 바뀝니다.")
    st.markdown(f"**{guide['next_action']}**")
    st.caption(guide["help"])
    st.caption(guide["baseline_note"])
    with st.container(key="wa_stages"):
        if narrow:
            for stage in guide["stages"]:
                st.markdown(
                    f"{stage['status']['text']} · {stage['label']} — {stage['help']}")
        else:
            cols = st.columns(len(guide["stages"]))
            for col, stage in zip(cols, guide["stages"]):
                col.metric(stage["label"], stage["status"]["text"])
                col.caption(stage["help"])
    with st.expander("이 단계에서 할 수 있는 일"):
        for action in guide["core_actions"]:
            st.write(f"· {action}")
        st.caption("위 일은 화면의 입력칸과 버튼으로 끝냅니다.")


def _table(guide, frame, zoom):
    try:
        window = timeline_focus(guide, int(frame), zoom)
    except FilmError as exc:
        st.error(str(exc))
        return
    st.caption(
        f"보이는 구간 [{window['start']}, {window['end']}) · "
        f"{window['window_rows']}프레임 · 표 {len(window['rows'])}행 · "
        f"선택 {window['frame']} (화면 번호 {window['frame'] + 1})")
    st.dataframe([{
        "프레임": row["frame_index"],
        "화면 번호": row["display"],
        "파일": row["file"],
        "컷": row["shot_id"] or "",
        "상태": row["status"],
        "선택": row["selection"],
    } for row in window["rows"]], hide_index=True, width="stretch")


def _save(p, frame, zoom, reduced, narrow, note, resume_ack):
    try:
        commit_workspace(
            p, frame=int(frame), zoom=zoom, reduced_motion=bool(reduced),
            narrow_layout=bool(narrow), note=note, resume_ack=bool(resume_ack))
    except FilmError as exc:
        st.error(str(exc))
        return
    st.session_state["wa_flash"] = "이 프레임과 메모를 기억했습니다."
    st.rerun()


def render_workspace(p):
    """The production workspace tab."""
    p = Path(p)
    st.subheader("제작 작업실")
    st.caption("준비 → 동작 → 검토 → 수정 → 출력. 다음에 할 일을 글로 보여 주고, "
               "긴 타임라인은 번호·버튼·확대로 이동합니다.")
    st.markdown(_REDUCED_CSS, unsafe_allow_html=True)
    flash = st.session_state.pop("wa_flash", None)
    if flash:
        st.success(flash)
    try:
        prefs = load_prefs(p)
        prefs_error = None
    except FilmError as exc:
        prefs = default_prefs()
        prefs_error = str(exc)
    if prefs_error:
        st.error("작업실 설정을 읽지 못했습니다. 화면의 값은 유지됩니다. "
                 + prefs_error)
    _init("wa_frame", int(prefs["selected_frame"]))
    _init("wa_zoom", prefs["zoom"])
    _init("wa_reduced", bool(prefs["reduced_motion"]))
    _init("wa_narrow", bool(prefs["narrow_layout"]))
    _init("wa_note", prefs["draft_note"])
    _init("wa_resume_ack", bool(prefs["resume_ack"]))

    guide = None
    guide_error = None
    try:
        guide = journey(p)
    except FilmError as exc:
        guide_error = str(exc)
    st.session_state["wa_total"] = (guide["output_frames"]
                                    if guide is not None else None)
    if guide is not None:
        try:
            st.session_state["wa_frame"] = clamp_frame(
                int(guide["output_frames"]),
                int(st.session_state["wa_frame"]))
        except (FilmError, TypeError, ValueError):
            pass

    cred = credential_view()
    restart = restart_view(p)
    _status_line(cred, restart)
    if guide_error:
        st.error("타임라인을 읽지 못했습니다. 입력과 저장된 프레임은 유지됩니다. "
                 + guide_error)

    narrow = st.checkbox(
        "좁은 화면에 맞추기", key="wa_narrow",
        help="단계를 가로 칸 대신 세로 목록으로 보여 줍니다. "
             "창 너비가 880px 이하면 가로 칸도 세로로 쌓입니다. "
             "넓은 데스크톱과 좁은 창을 같은 글로 읽습니다.")
    reduced = st.checkbox(
        "동작 줄이기", key="wa_reduced",
        help="화면 전환 애니메이션을 끕니다. 프레임이 이동하는 칸 수는 바뀌지 않습니다.")
    if reduced:
        st.markdown(_REDUCED_ON_CSS + '<div class="wa-reduced-on">동작 줄이기 켜짐</div>',
                    unsafe_allow_html=True)
        st.caption("동작 줄이기 켜짐 — 화면 이동 애니메이션 없음. "
                   "프레임 번호는 그대로 이동합니다.")
    else:
        st.caption("동작 줄이기 꺼짐. 운영체제가 동작 줄이기를 요청하면 "
                   "애니메이션은 따라 꺼집니다.")

    if guide is not None:
        _journey_block(guide, narrow)
    saved = prefs["selected_frame"] if prefs.get("stored") else None
    if saved is None:
        st.caption("저장된 선택 프레임: 아직 없음")
    else:
        st.caption(f"저장된 선택 프레임: {saved} "
                   f"(화면 번호 {saved + 1}, F_{saved + 1:06d}.png)")
    if prefs.get("stored") and prefs.get("draft_note"):
        st.caption("저장된 메모가 있습니다. 입력이 거절되어도 그 메모는 남습니다.")

    hi = (guide["output_frames"] - 1) if guide is not None else max(
        int(prefs["selected_frame"]), int(st.session_state["wa_frame"]), 0)
    st.caption("키보드: Tab으로 좁은 화면, 동작 줄이기, 프레임 번호, 확대 단계, "
               "메모, 이동 버튼, 기억 버튼 순서로 이동합니다. "
               "프레임 번호는 숫자와 화살표 키, 버튼은 Enter입니다. "
               "실제 키 입력 순서는 이 환경에서 확인하지 않았습니다.")
    frame = st.number_input(
        "프레임 번호 (0부터)", min_value=0, max_value=int(hi), step=1,
        key="wa_frame",
        help="0이 첫 프레임입니다. 화면 번호는 1부터입니다. "
             "화살표 키로 한 프레임씩 움직입니다.")
    zoom = st.selectbox(
        "확대 단계", list(ZOOM_ORDER), key="wa_zoom",
        format_func=lambda key: _ZOOM_LABEL.get(key, key),
        help="넓게는 긴 구간, 한 프레임은 가장 크게 본 상태입니다.")
    c1, c2 = st.columns(2)
    c1.button("확대", key="wa_zoom_in", on_click=_zoom, args=("in",),
              help="보이는 구간을 더 좁힙니다.")
    c2.button("축소", key="wa_zoom_out", on_click=_zoom, args=("out",),
              help="보이는 구간을 더 넓힙니다.")
    note = st.text_area(
        "메모", key="wa_note", height=80,
        help="다음에 볼 내용을 적습니다. 2000자까지입니다. "
             "저장이 거절되면 이전에 기억한 메모와 프레임이 남고, "
             "지금 입력한 글은 칸에 그대로 있습니다.")
    if guide is not None:
        _table(guide, frame, zoom)
        moves = st.columns(6)
        buttons = (
            (0, "이전 프레임", "wa_prev", "previous_frame"),
            (1, "다음 프레임", "wa_next", "next_frame"),
            (2, "이전 구간", "wa_page_prev", "previous_page"),
            (3, "다음 구간", "wa_page_next", "next_page"),
            (4, "처음으로", "wa_home", "home"),
            (5, "끝으로", "wa_end", "end"),
        )
        for index, label, key, move in buttons:
            moves[index].button(
                label, key=key, on_click=_nudge, args=(move,),
                help="프레임 번호만 바꿉니다. 기억 버튼을 눌러야 저장됩니다.")
    resume_ack = bool(st.session_state.get("wa_resume_ack"))
    if restart["state"] == "RESTART":
        st.caption("재시작 대기입니다. 확인은 기록만 하고 같은 작업을 다시 보내지 않습니다.")
        if st.button("같은 작업을 이어서 확인", key="wa_resume",
                     help="확인만 기억합니다. 작업을 다시 제출하지 않습니다."):
            resume_ack = True
            st.session_state["wa_resume_ack"] = True
            _save(p, frame, zoom, reduced, narrow, note, True)
            return
    if st.button("이 프레임 기억", key="wa_save",
                 help="프레임 번호, 확대, 동작 줄이기, 메모를 함께 저장합니다."):
        _save(p, frame, zoom, reduced, narrow, note, resume_ack)
