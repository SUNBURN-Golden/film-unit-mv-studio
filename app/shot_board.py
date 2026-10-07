"""FILM shot board — 컷 작업대 ("10 · 컷 작업대" tab).

Rendered only for FRAME_ANIMATION_V1 projects (`control_panel.render`
gates the tab on the production profile); LEGACY_MV projects render
exactly as before. The board is the film-shot-board node: storyboard and
timeline share one stable `instance_id` selection, the boundary/transition
ownership fixture lists per-frame contributors straight from
`engine.shot_board.boundary_window`, and the draft editor compares a
numeric edit against the adopted cut before writing anything.

Every number on screen is the integer frame clock's own vocabulary —
0-based `frame_index`, 1-based display numbers, `F_#######` file names and
exact rational exposure windows — so the displayed frame is the frame the
compiler writes. Numeric editing uses `st.number_input` (keyboard-arrows
friendly) and navigation uses ordinary buttons/selectboxes; no custom
JavaScript is needed and none is added. The lyrics banner reports the
current cue/timing hashes so a cut edit visibly leaves them alone.

Everything imported through this panel stays DRAFT and every state shown
is recomputed from project bytes: qualification UNQUALIFIED, acceptance
PENDING, release NOT_AUTHORIZED — the board never implies production
release. Real-browser rendering and keyboard focus order are not exercised
by AppTest and stay unverified.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError, safe_path
from engine.shot_board import (CROSSFADE, FUND_SIDES, HARD_CUT, apply_edit,
                               board_entry, board_view, boundary_window,
                               propose_edit)

_STATE_KO = {"CURRENT": "현재", "STALE": "낡음", "UNREVIEWED": "미검수",
             "UNRESOLVED": "미해결", "CHANGES_REQUIRED": "수정 필요",
             "UNLOCKED": "미잠금"}

_TRANSITION_KO = {"HARD_CUT": "하드컷", "CROSSFADE": "크로스페이드"}
_FUND_KO = {"OUTGOING": "앞 컷의 뒤 여유분", "INCOMING": "뒤 컷의 앞 여유분"}


def _guarded(fn):
    """Run one engine call; a refusal becomes a message, not a traceback."""
    try:
        return fn()
    except FilmError as exc:
        st.error(str(exc))
    except Exception as exc:  # a broken project file must not blank the tab
        st.error(f"처리하지 못했습니다: {exc}")
    return None


def _ratio(pair):
    """`[num, den]` -> 'num/den s' display (1/1 rendered as whole seconds)."""
    num, den = pair
    return f"{num}" if den == 1 else f"{num}/{den}"


def _state(text):
    return _STATE_KO.get(text, text)


def _select_frame(view, value):
    """The entry whose output range covers display frame `value`."""
    for row in view["entries"]:
        if row["display_range"][0] <= value <= row["display_range"][1]:
            return row["instance_id"]
    return None


def _strip(view):
    """Storyboard cards in timeline order; selection is the instance id."""
    per_row = 6
    for start in range(0, len(view["entries"]), per_row):
        cols = st.columns(min(per_row, len(view["entries"]) - start))
        for col, row in zip(cols, view["entries"][start:start + per_row]):
            with col:
                image = row["storyboard"]["path"]
                if image:
                    col.image(str(safe_path(view["project"], image)))
                col.caption(f"{row['shot_id']} · {row['instance_id']} · "
                            f"표시 {row['display_range'][0]}–"
                            f"{row['display_range'][1]}")
                if col.button("선택", key=f"sb_pick_{row['instance_id']}"):
                    st.session_state["sb_instance"] = row["instance_id"]
                    st.rerun()


def _detail(p, view, row):
    st.markdown(
        f"**{row['instance_id']} · {row['shot_id']}** — "
        f"출력 `[{row['output_range'][0]}, {row['output_range'][1]})` "
        f"({row['length_frames']}프레임) · 표시 "
        f"{row['display_range'][0]}–{row['display_range'][1]} · 노출 "
        f"{_ratio(row['exposure_window']['start'])}–"
        f"{_ratio(row['exposure_window']['end'])}초")
    c1, c2 = st.columns(2)
    c1.caption(
        f"소스 구간 `[{row['used_source_range'][0]}, "
        f"{row['used_source_range'][1]})` · 여유분 앞 "
        f"{row['unused_handles']['before']} / 뒤 "
        f"{row['unused_handles']['after']} 프레임 · 채택 리비전 "
        f"r{row['sequence_revision']}")
    pin = row["pin"]
    if pin["resolved"]:
        c1.caption(f"자산 {pin['asset_id']} r{pin['revision']} · "
                   f"{pin['kind']} {pin['member_count']}프레임 · "
                   f"{pin['content_sha256'][:12]}… · DRAFT")
    else:
        c1.warning(f"해결되지 않은 컷: {pin.get('error', '알 수 없는 오류')}")
    c2.caption(f"컷 검수: {_state(row['review'])}"
               + (f" · 전환 검수: {_state(row['transition_review'])}"
                  if row["transition_review"] else ""))
    if row["draft_frames"]["recorded"]:
        c2.caption(f"draft 프레임 원장: "
                   f"{row['draft_frames']['recorded']}개 기록 "
                   "(채택과 별도 — 승인 아님)")
    plan = row["plan"]
    if plan and "error" in plan:
        c2.warning(f"샷 계획이 현재 컷 길이와 맞지 않습니다: {plan['error']}")
    elif plan:
        layers = ", ".join(
            f"{lid}·{t['drawings']}그림·{t['exposure_slots']}슬롯"
            for lid, t in plan["layers"].items()) or "레이어 없음"
        c2.caption(f"샷 계획: {layers} · 카메라 "
                   f"{'움직임' if plan['camera'] else '고정'}")
    if row["storyboard"]["description"]:
        st.caption(f"콘티 메모: {row['storyboard']['description']}")


def _transition_table(view):
    if not view["transitions"]:
        st.info("컷이 하나뿐이라 전환이 없습니다.")
        return
    st.dataframe(
        [{"전환": b["id"], "종류": _TRANSITION_KO.get(b["type"], b["type"]),
          "기록 소유": b["owner"],
          "연결": f"{b['from_instance']} → {b['to_instance']}",
          "겹침": b["overlap_frames"],
          "출력 범위": (f"[{b['output_range'][0]}, {b['output_range'][1]})"
                      if b["output_range"][1] > b["output_range"][0]
                      else "—"),
          "표시 프레임": (f"{b['display_range'][0]}–{b['display_range'][1]}"
                        if b["display_range"] else "—"),
          "경계 프레임":
              f"{(b['boundary_frames']['before'] or {}).get('file') or '—'}"
              " ↔ "
              f"{(b['boundary_frames']['after'] or {}).get('file') or '—'}",
          "검수": _state(b["review"])}
         for b in view["transitions"]], hide_index=True)


def _boundary_fixture(p, view, row):
    """경계 프레임·전환 소유권 UI fixture for the selected cut's transition."""
    transition = row["transition_out"]
    if transition is None:
        st.info("선택한 컷은 마지막 컷이라 뒤 전환이 없습니다 — "
                "앞 컷을 고르면 그 경계를 볼 수 있습니다.")
        return
    index = next(b["index"] for b in view["transitions"]
                 if b["id"] == transition["id"])
    window = _guarded(lambda: boundary_window(p, index))
    if window is None:
        return
    st.markdown(
        f"**{window['transition']['id']}** "
        f"({_TRANSITION_KO.get(window['transition']['type'])}) · "
        f"기록 소유 **{window['record_owner']}** (`transition_out`) · "
        f"겹침 구간 합성 소유 **{window['overlap_op_owner']}** "
        f"(앞 컷 꼬리는 halo로 읽힘)")
    st.dataframe(
        [{"프레임": f["display"], "내부": f["frame_index"],
          "파일": f["file"],
          "시각": f"{_ratio(f['time']['start'])}–{_ratio(f['time']['end'])}초",
          "합성 소유": f["op_owner"],
          "전환": f["transition"] or "—",
          "기여": ", ".join(
              f"{c['instance_id']} 멤버 {c['member']}"
              + (f" ×{c['weight'][0]}/{c['weight'][1]}"
                 if c['weight'] != [1, 1] else "")
              + (f" ({'앞' if c['side'] == 'outgoing' else '뒤' if c['side'] == 'incoming' else '단독'})")
              for c in f["contributors"])}
         for f in window["frames"]], hide_index=True)
    # Boundary-frame images: last outgoing-only and first incoming-only.
    before, after = (window["boundary_frames"]["before"],
                     window["boundary_frames"]["after"])
    cols = st.columns(2)
    for col, frame_index, label in (
            (cols[0], before, "앞 컷 마지막 단독 프레임"),
            (cols[1], after, "뒤 컷 첫 단독 프레임")):
        if frame_index is None:
            continue
        for f in window["frames"]:
            if f["frame_index"] != frame_index:
                continue
            sole = f["contributors"][0]
            member_path = sole.get("member_path")
            if member_path and safe_path(p, member_path).is_file():
                col.image(str(safe_path(p, member_path)),
                          caption=f"{label}: {f['file']} · "
                                  f"{sole['instance_id']} 멤버 "
                                  f"{sole['member']}")
            else:
                col.caption(f"{label}: {f['file']} · 멤버 {sole['member']} "
                            "(미해결 — 이미지 없음)")


def _diff_table(draft):
    rows = []
    for change in draft["diff"]:
        adopted, candidate = change["adopted"], change["draft"]
        tr_a = adopted["transition_out"] or {}
        tr_b = candidate["transition_out"] or {}
        rows.append({
            "컷": f"{change['instance_id']} · {change['shot_id']}",
            "채택 출력": str(adopted["output_range"]),
            "초안 출력": str(candidate["output_range"]),
            "채택 소스": str(adopted["used_source_range"]),
            "초안 소스": str(candidate["used_source_range"]),
            "채택 여유분": str(adopted["unused_handles"]),
            "초안 여유분": str(candidate["unused_handles"]),
            "채택 전환": (f"{tr_a.get('type')}×{tr_a.get('overlap_frames')}"
                        if tr_a else "—"),
            "초안 전환": (f"{tr_b.get('type')}×{tr_b.get('overlap_frames')}"
                       if tr_b else "—")})
    st.dataframe(rows, hide_index=True)


def _projection_table(projection):
    rows = []
    for target, entry in sorted(projection["reviews"].items()):
        if entry["now"] == entry["after"]:
            continue
        rows.append({"범위": f"{entry['scope']} {target}",
                     "지금": _state(entry["now"]),
                     "초안 적용 후": _state(entry["after"])})
    for name, entry in sorted(projection["locks"].items()):
        if entry["now"] == entry["after"]:
            continue
        rows.append({"범위": name, "지금": _state(entry["now"]),
                     "초안 적용 후": _state(entry["after"])})
    if rows:
        st.warning("이 초안을 적용하면 낡아지는 검수·잠금이 있습니다 "
                   "(기존 승인은 새 초안에 승계되지 않습니다):")
        st.dataframe(rows, hide_index=True)
    else:
        st.caption("낡아지는 검수·잠금이 없습니다.")


def _draft_editor(p, view, row):
    st.markdown("**초안 편집 — 채택 컷과 비교한 뒤 적용**")
    if row["position"] >= len(view["entries"]) - 1:
        st.info("마지막 컷에는 뒤 경계·전환이 없습니다. 앞 컷을 선택하세요.")
        return
    current = row["transition_out"]
    c1, c2 = st.columns(2)
    shift = int(c1.number_input(
        f"경계 이동 (프레임 — +는 {row['instance_id']} 출력을 늘립니다)",
        min_value=-100000, max_value=100000, value=0, step=1,
        key="sb_shift"))
    kind = c2.selectbox("전환 종류",
                        ["유지", HARD_CUT, CROSSFADE], key="sb_ttype")
    transition = None
    if kind != "유지":
        c3, c4 = st.columns(2)
        overlap = int(c3.number_input(
            "겹침 프레임 (정수)", min_value=0, max_value=100000,
            value=int(current["overlap_frames"]), step=1, key="sb_overlap"))
        fund = c4.radio("겹침 프레임 출처", list(FUND_SIDES),
                        format_func=lambda f: _FUND_KO[f], key="sb_fund")
        if kind != current["type"] or overlap != current["overlap_frames"]:
            transition = {"type": kind, "overlap_frames": overlap,
                          "fund": fund}
    if st.button("초안 만들기·비교", key="sb_propose"):
        draft = _guarded(lambda: propose_edit(
            p, row["instance_id"],
            boundary_shift=shift or None, transition=transition))
        if draft is not None:
            st.session_state["sb_draft"] = draft
            st.rerun()
    draft = st.session_state.get("sb_draft")
    if not draft:
        st.caption("아직 만든 초안이 없습니다.")
        return
    if draft["instance_id"] != row["instance_id"]:
        st.caption("선택한 컷의 초안이 아닙니다 — 초안을 만들거나 버리세요.")
        return
    if not draft["valid"]:
        st.markdown("**초안 diff — 채택 대비 변경**")
        for error in draft["errors"]:
            st.error(error)
        st.caption("유효하지 않은 초안은 적용할 수 없습니다.")
    else:
        st.markdown(f"**초안 diff — 채택 대비 변경 "
                    f"{len(draft['diff'])}컷**")
        _diff_table(draft)
        _projection_table(draft["projection"])
        closure = draft.get("closure") or {}
        if "unavailable" in closure:
            st.caption("다시 계산할 범위를 구하지 못했습니다: "
                       + closure["unavailable"])
        elif closure.get("dirty_frames") is not None:
            st.caption(f"다시 합성할 출력 프레임 "
                       f"{len(closure['dirty_frames'])}개 · "
                       f"총 출력 {closure['output_frames']['before']} → "
                       f"{closure['output_frames']['after']} 프레임 · "
                       f"인코딩 {', '.join(closure.get('encode_roles', [])) or '없음'}")
        st.caption(f"가사 문서 변경 없음 — timing "
                   f"{(draft['lyrics']['timing_sha256'] or '없음')[:12]}…")
    c1, c2 = st.columns(2)
    if c1.button("이 초안 적용", key="sb_apply", disabled=not draft["valid"]):
        result = _guarded(lambda: apply_edit(p, draft))
        if result is not None:
            st.session_state.pop("sb_draft", None)
            st.session_state["sb_flash"] = (
                f"적용됨: {result['output_frames']}프레임 유지 · "
                f"낡은 범위 {len(result['stale'])}개 · "
                f"가사 {'변경 없음' if result['lyrics']['unchanged'] else '변경됨'}")
            st.rerun()
    if c2.button("초안 버리기", key="sb_discard"):
        st.session_state.pop("sb_draft", None)
        st.rerun()


def render_shot_board(p):
    """The "10 · 컷 작업대" tab body."""
    st.caption("컷을 고르면 시간축·프레임·검수·자산 사용처가 함께 움직입니다. "
               "모든 표시는 정수 프레임 계약을 따르며, 컷 편집은 가사 cue를 "
               "옮기지 않습니다. synthetic protocol only — qualification "
               "UNQUALIFIED · acceptance PENDING · release NOT_AUTHORIZED")
    flash = st.session_state.pop("sb_flash", None)
    if flash:
        st.success(flash)
    view = _guarded(lambda: board_view(p))
    if view is None:
        return
    cols = st.columns(5)
    cols[0].metric("컷", len(view["entries"]))
    cols[1].metric("출력 프레임", view["output_frames"])
    cols[2].metric("정수 클럭", f"{view['fps']}fps")
    cols[3].metric("겹침 프레임", view["overlap_frames"])
    stale = view["review_counts"].get("STALE", 0)
    cols[4].metric("낡은 검수", stale)
    coverage = view["coverage"]
    if coverage["uncovered_frames"] or coverage["frames_with_three_or_more"]:
        st.error("공백/삼중 겹침이 있습니다: "
                 f"미커버 {coverage['uncovered_frames']} · "
                 f"삼중 {coverage['frames_with_three_or_more']}")
    if coverage["shared_frames"]:
        st.caption(f"전환 공유 프레임 {coverage['shared_frames']}개")
    lyric = view["lyrics"]
    st.caption(f"가사 cue {lyric['cues']}개 · timing "
               f"{(lyric['timing_sha256'] or '없음')[:12]}… · "
               f"잠금 PLAN {_state(view['locks']['plan'])} · FINAL "
               f"{_state(view['locks']['final'])}")

    st.markdown("**콘티 — 타임라인 순서**")
    _strip(view)

    instances = view["instances"]
    if st.session_state.get("sb_instance") not in instances:
        st.session_state["sb_instance"] = instances[0]
    by_instance = {r["instance_id"]: r for r in view["entries"]}
    pick = st.selectbox(
        "컷", instances, key="sb_instance",
        format_func=lambda i: (
            f"{i} · {by_instance[i]['shot_id']} · 출력 "
            f"[{by_instance[i]['output_range'][0]}, "
            f"{by_instance[i]['output_range'][1]}) 프레임"))
    row = board_entry(view, pick)

    def _jump():
        owner = _select_frame(view, int(st.session_state["sb_jump"]))
        if owner is not None:
            st.session_state["sb_instance"] = owner

    c1, c2 = st.columns([3, 1])
    c1.number_input(
        "출력 프레임으로 이동 (표시 번호)", min_value=1,
        max_value=max(1, view["output_frames"]), value=1, step=1,
        key="sb_jump")
    c2.button("이동", key="sb_jump_button", on_click=_jump)

    st.markdown("**선택한 컷**")
    _detail(p, view, row)

    st.markdown("**경계·전환 소유권**")
    _transition_table(view)
    _boundary_fixture(p, view, row)

    st.markdown("---")
    _draft_editor(p, view, row)
