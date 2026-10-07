"""film-lyrics-review — 가사·한글 자막 검토 작업대 ("11 · 가사 검토" tab).

Rendered for both profiles — the lyric contract is shared: LEGACY_MV
projects show the production-LOCK/fingerprint invalidation scope and
FRAME_ANIMATION_V1 projects show the ANIM-020 subbed-only closure. The
panel only reads `engine.lyrics_review` results:

- a listening position (original-master ms) selects the cue it lands on,
  with the flanking cues and the integer frame it maps to;
- the cue table explains repeat expansions, back-to-back/overlapping
  seams, coverage gaps, long lines and missing glyphs;
- the source diff shows an un-prepared source edit and the source the
  archived timing was authored on;
- the invalidation section names exactly which reviews and locks a
  timing/subtitle/font/source-text change makes stale — and that clean
  and subbed masters are separate artifacts;
- the candidate section evaluates an imported/ASR timing document
  without writing; a review is recorded only when a named human checks
  the real-vocal confirmation box.

All records remain synthetic protocol records: qualification UNQUALIFIED,
acceptance PENDING, release NOT_AUTHORIZED. Real-browser rendering and
keyboard focus order are not exercised by AppTest and stay unverified.
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError, project_mutex, safe_path
from engine.lyrics import save_timing
from engine.lyrics_review import (CHANGE_KINDS, cue_at, evaluate_candidate,
                                  invalidation_report, workbench)

_STATE_KO = {"CURRENT": "현재", "STALE": "낡음", "UNREVIEWED": "미검수",
             "UNLOCKED": "미잠금", "UNRESOLVED": "미해결",
             "UNVERIFIABLE": "확인 불가", "KEPT": "유지",
             "verified": "검증됨", "missing_glyphs": "글리프 없음",
             "unverified": "미검증", "error": "오류"}
_RELATION_KO = {"OVERLAP": "겹침", "BACK_TO_BACK": "붙은 cue",
                "TIGHT_GAP": "촘촘한 틈", "GAP": "틈"}
_FLAG_KO = {"LONG_LINE": "긴 줄", "FAST_READ": "빠른 읽기",
            "SHORT": "짧은 표시", "MISSING_GLYPHS": "글리프 없음"}
_CHANGE_KO = {"TIMING": "cue 타이밍 변경",
              "SUBTITLE_SETTINGS": "자막 설정 변경 (크기·여백·글꼴명)",
              "FONT": "폰트 파일 변경",
              "SOURCE_TEXT": "가사 원문 변경"}
_POSITION_KO = {"ON_CUE": "cue 위", "IN_GAP": "cue 사이",
                "BEFORE_FIRST": "첫 cue 이전", "AFTER_LAST": "마지막 cue 이후"}


def _guarded(fn):
    try:
        return fn()
    except FilmError as exc:
        st.error(str(exc))
    except Exception as exc:  # a broken project file must not blank the tab
        st.error(f"처리하지 못했습니다: {exc}")
    return None


def _state(text):
    return _STATE_KO.get(text, text)


def _flag_text(flags):
    return " · ".join(_FLAG_KO.get(f, f) for f in flags) or "—"


def _cue_table(board):
    if not board["cues"]:
        st.info("아직 타이밍된 cue가 없습니다. LYRICS 탭에서 타이밍 문서를 "
                "준비·저장하세요.")
        return
    st.dataframe(
        [{"cue": c["id"], "행": c["source_row_id"],
          "시각": c["timecode"],
          "프레임": f"{c['frames'][0]}–{c['frames'][1]}",
          "텍스트": c["text"],
          "글자": c["chars"],
          "글자/초": c["chars_per_second"],
          "예상 줄": c["est_lines"],
          "표시": _flag_text(c["flags"])}
         for c in board["cues"]], hide_index=True)


def _seam_table(board):
    seams = [s for s in board["seams"] if s["relation"] != "GAP"]
    if not seams:
        st.caption("cue 사이에 겹침·붙은 경계·한 프레임 미만의 틈이 없습니다.")
        return
    st.caption("겹침·붙은 경계·한 프레임 미만의 틈 — 실제 보컬로 다시 "
               "들어 볼 지점입니다:")
    st.dataframe(
        [{"위치": s["at_timecode"], "앞 cue": s["left_id"],
          "뒤 cue": s["right_id"], "틈(ms)": s["gap_ms"],
          "관계": _RELATION_KO.get(s["relation"], s["relation"])}
         for s in seams], hide_index=True)


def _repeat_section(board):
    if not board["repeats"]:
        return
    st.markdown("**반복 구절 — 명시적 확장만 인정됩니다**")
    for rep in board["repeats"]:
        state = "해결됨" if rep["resolved"] else "미해결"
        st.caption(f"`{rep['marker_row_id']}` [{rep['section']}] — {state} · "
                   f"원문 구간 {', '.join(rep['suggested_source_row_ids'])}")
        st.dataframe(
            [{"원문 행": e["row_id"], "확장 ID": e["effective_id"],
              "텍스트": e["text"],
              "타이밍": "있음" if e["timed"] else "없음"}
             for e in rep["expanded_rows"]], hide_index=True)


def _font_section(board):
    font = board["font"]
    report = font["report"]
    st.markdown("**폰트 coverage — 실제 cue 문자 기준 cmap 검사**")
    cols = st.columns(4)
    cols[0].metric("상태", _state(report["status"]))
    cols[1].metric("글꼴", report.get("family") or "—")
    cols[2].metric("빠진 문자", len(report.get("missing_codepoints") or []))
    cols[3].metric("Final", "차단" if font["final_blocked"] else "가능")
    if report.get("note"):
        st.warning(report["note"])
    if report.get("missing_codepoints"):
        st.error("이 글꼴로는 Final이 나가지 않습니다: "
                 + ", ".join(report["missing_codepoints"][:20]))
        if font["missing_by_cue"]:
            st.dataframe(
                [{"cue": cue, "빠진 문자": ", ".join(chars)}
                 for cue, chars in font["missing_by_cue"].items()],
                hide_index=True)
    st.caption("글꼴이 검증돼도 긴 줄·줄바꿈·읽기 속도는 사람이 Preview로 "
               "확인해야 합니다.")


def _diff_table(diff):
    rows = []
    for change in diff["changes"]:
        rows.append({"변경": {"replace": "바뀜", "delete": "삭제",
                              "insert": "추가"}.get(change["op"],
                                                    change["op"]),
                     "이전 행": ", ".join(f"L{l:04d}" for l in
                                          change["before_lines"]) or "—",
                     "현재 행": ", ".join(f"L{l:04d}" for l in
                                          change["after_lines"]) or "—",
                     "이전": " / ".join(change["before"]) or "—",
                     "현재": " / ".join(change["after"]) or "—"})
    st.dataframe(rows, hide_index=True)


def _diff_section(board):
    diff = board["source_diff"]
    with st.expander("가사 원문 diff", expanded=bool(
            (diff.get("pending") or {}).get("changed")
            or (diff.get("invalidated") or {}).get("changed"))):
        pending = diff.get("pending")
        if pending:
            st.warning(pending["note"])
            _diff_table(pending)
        else:
            st.caption("준비된 원문과 현재 원문이 같습니다.")
        invalidated = diff.get("invalidated")
        if invalidated:
            st.info(invalidated["note"])
            _diff_table(invalidated)
        else:
            st.caption("폐기된 타이밍 원문이 없습니다 — 이 원문의 첫 "
                       "타이밍이거나 원문이 바뀌지 않았습니다.")


def _position_section(p, board):
    st.markdown("**청취 위치 → cue 선택**")
    st.caption("사이드바 음원을 들으며 위치를 옮기면 그 시각의 cue가 "
               "선택됩니다. 시간은 원곡 기준 ms이며 컷 편집과 무관합니다.")
    if not board["cues"] or board["duration_ms"] is None:
        st.info("음원 분석과 타이밍 문서가 있어야 청취 위치를 연결할 수 "
                "있습니다.")
        return
    c1, c2 = st.columns([3, 1])
    c1.number_input("청취 위치 (원곡 ms)", min_value=0,
                    max_value=max(0, board["duration_ms"]), value=0,
                    step=100, key="lr_position")
    jump = c2.button("이 위치의 cue", key="lr_jump")
    if jump:
        result = _guarded(lambda: cue_at(
            {"cues": board["cues"]}, int(st.session_state["lr_position"]),
            fps=board["fps"]))
        if result is not None:
            st.session_state["lr_position_result"] = result
    result = st.session_state.get("lr_position_result")
    if result:
        cue = result["cue"]
        label = (f"{result['position_timecode']} (프레임 "
                 f"{result['position_frame']}) — "
                 f"{_POSITION_KO.get(result['state'], result['state'])}")
        if cue:
            st.success(f"{label} → **{cue['id']}** “{cue['text']}” "
                       f"[{cue['start_ms']}–{cue['end_ms']} ms]")
            st.session_state["lr_cue"] = cue["id"]
        else:
            flank = []
            if result["previous"]:
                flank.append(f"앞 {result['previous']['id']}"
                             f" (+{result['gap_before_ms']} ms)")
            if result["next"]:
                flank.append(f"뒤 {result['next']['id']}"
                             f" (−{result['gap_after_ms']} ms)")
            st.info(f"{label} — " + (" · ".join(flank) or "cue 없음"))
    ids = [c["id"] for c in board["cues"]]
    if st.session_state.get("lr_cue") not in ids:
        st.session_state["lr_cue"] = ids[0]
    picked = st.selectbox("cue", ids, key="lr_cue",
                          format_func=lambda i: (
                              f"{i} · "
                              f"{next(c['timecode'] for c in board['cues'] if c['id'] == i)} · "
                              f"{next(c['text'] for c in board['cues'] if c['id'] == i)[:24]}"))
    row = next(c for c in board["cues"] if c["id"] == picked)
    st.caption(f"선택한 cue `{row['id']}` — 원문 행 {row['source_row_id']} · "
               f"{row['start_ms']}–{row['end_ms']} ms · 출력 프레임 "
               f"{row['frames'][0]}–{row['frames'][1]} · "
               f"{row['chars']}자 · {row['chars_per_second']}자/초 · "
               f"표시 {_flag_text(row['flags'])}")


def _invalidation_section(p, board):
    st.markdown("**자막 변경 → 재검수·LOCK 범위**")
    st.caption(board["builds"] and board["builds"][0]["re_review_scope"]
               or "자막·폰트 변경은 clean과 subbed를 같은 결과로 합치지 "
                 "않습니다.")
    change = st.selectbox("변경 종류", list(CHANGE_KINDS),
                          format_func=lambda c: _CHANGE_KO[c],
                          key="lr_change")
    report = _guarded(lambda: invalidation_report(p, change))
    if report is None:
        return
    st.caption(report["clean_vs_subbed"])
    now = report.get("locks_now") or {}
    if "unavailable" not in now:
        if board["profile"] == "FRAME_ANIMATION_V1":
            waves = ", ".join(f"{w} {_state(s)}"
                              for w, s in (now.get("waves") or {}).items())
            st.caption(f"현재 잠금 — PLAN {_state(now.get('plan'))} · "
                       f"FINAL {_state(now.get('final'))}"
                       + (f" · {waves}" if waves else ""))
        else:
            st.caption(f"현재 잠금 — production LOCK "
                       f"{_state(now.get('production'))}")
    proj = report["projection"]
    if proj.get("unavailable"):
        st.warning("정확한 closure를 계산하지 못해 binding 수준으로 "
                   "표시합니다: " + proj["unavailable"])
    st.caption(proj["reason"])
    rows = [{"범위": name, "변경 후": "낡음"} for name in proj["stale"]]
    rows += [{"범위": name, "변경 후": "유지"} for name in proj.get("kept", [])]
    if rows:
        st.dataframe(rows, hide_index=True)
    closure = proj.get("closure") or {}
    if closure.get("subbed_dirty_frames") is not None:
        st.caption(f"clean 재합성 프레임 {len(closure['dirty_frames'])} · "
                   f"subbed 재합성 프레임 "
                   f"{len(closure['subbed_dirty_frames'])} · "
                   f"다시 인코딩: {', '.join(closure['encode_roles']) or '없음'}")
    st.caption("가사 검수는 언제나 사람이 다시 들어 확인해야 복구됩니다 "
               "— 어떤 변경도 기존 검토를 새 문서에 승계하지 않습니다.")


def _builds_section(board):
    if not board["builds"]:
        st.caption("아직 sealed 빌드가 없습니다.")
        return
    st.markdown("**sealed 빌드 — clean / subbed 분리**")
    st.dataframe(
        [{"빌드": b["build_id"], "모드": b.get("mode") or "—",
          "clean sha": (b["clean_sha256"] or "—")[:12],
          "subbed sha": (b["subbed_sha256"] or "—")[:12],
          "구분": "별개 결과물" if b["distinct_outputs"] else "—",
          "clean seq": ((b["sequences"]["clean_sequence_root"] or "—")[:12]),
          "subbed seq": ((b["sequences"]["subbed_sequence_root"] or "—")[:12]),
          "봉인된 가사 검수": ((b["lyrics"]["lyrics_review_sha256"] or "—")[:12]),
          "현재 검수와 일치":
              {True: "일치", False: "다름", None: "—"}[
                  b["review_matches_current"]]}
         for b in board["builds"]], hide_index=True)
    st.caption("자막·폰트 변경은 sealed 빌드 바이트를 바꾸지 않습니다. 새 "
               "subbed 전달물은 새 빌드·새 검토가 필요하고, clean 마스터는 "
               "그 변경의 재검수 범위 밖입니다.")


def _candidate_section(p):
    st.markdown("**ASR·외부 정렬 후보 검사 — 승인 없이 미리보기**")
    st.caption("가져온 후보는 저장·승인되지 않습니다. 후보 안의 review "
               "필드는 무시되며, 사람이 실제 보컬과 대조해 검토자로 저장할 "
               "때만 검수가 기록됩니다.")
    upload = st.file_uploader("후보 lyrics_timed.json", type=["json"],
                              key="lr_candidate_file")
    text = st.text_area("또는 후보 JSON 붙여넣기", height=160,
                        key="lr_candidate_text")
    if st.button("후보 검사", key="lr_eval"):
        content = upload.getvalue().decode("utf-8") if upload else text

        def evaluate():
            return evaluate_candidate(p, json.loads(content))
        result = _guarded(evaluate)
        if result is not None:
            st.session_state["lr_candidate"] = {
                "report": result, "document": json.loads(content)}
            st.session_state.pop("lr_candidate_saved", None)
    saved = st.session_state.pop("lr_candidate_saved", None)
    if saved:
        st.success(saved)
    held = st.session_state.get("lr_candidate")
    if not held:
        return
    report = held["report"]
    if report["candidate_review_ignored"]:
        st.warning("후보에 review 필드가 있었습니다 — 무시했습니다. 가져온 "
                   "후보는 검토 완료가 아닙니다.")
    for error in report["errors"]:
        st.error(error)
    for warning in report["warnings"]:
        st.warning(warning)
    if report["valid"]:
        st.success("후보가 형식 검사를 통과했습니다 — 아직 미검수 상태입니다.")
        analysis = report["analysis"] or {}
        if analysis.get("coverage_gaps"):
            st.caption(f"타이밍되지 않은 원문 구간 "
                       f"{len(analysis['coverage_gaps'])}곳이 남아 있습니다 — "
                       "검토자 승인은 전 행이 해결돼야 합니다.")
        seams = [s for s in report.get("seams", [])
                 if s["relation"] in ("OVERLAP", "BACK_TO_BACK", "TIGHT_GAP")]
        if seams:
            st.dataframe(
                [{"위치": s["at_timecode"], "앞 cue": s["left_id"],
                  "뒤 cue": s["right_id"], "틈(ms)": s["gap_ms"],
                  "관계": _RELATION_KO.get(s["relation"], s["relation"])}
                 for s in seams], hide_index=True)
    reviewer = st.text_input("자막 검토자", key="lr_candidate_reviewer")
    reviewed = st.checkbox(
        "실제 보컬과 구절의 시작·끝, 누락·반복 및 자막 표시를 들어 "
        "확인했습니다.", key="lr_candidate_reviewed")
    if st.button("후보 저장", key="lr_candidate_save"):
        def save():
            if reviewed and not reviewer.strip():
                raise FilmError("검토자 이름을 입력해주세요.")
            with project_mutex(p):
                return save_timing(p, held["document"],
                                   reviewer=reviewer if reviewed else "")
        result = _guarded(save)
        if result is not None:
            st.session_state["lr_candidate_saved"] = (
                "저장했습니다 — 검토가 기록됐습니다"
                if result.get("review") else
                "저장했습니다 — 검토자가 없어 미검수로 남았습니다")
            st.session_state.pop("lr_candidate", None)
            st.rerun()


def render_lyrics_review(p):
    """The "11 · 가사 검토" tab body."""
    st.caption("실제 보컬을 들으며 가사 cue와 한글 자막 읽기 품질을 "
               "검토합니다. 원문·기존 검토 cue·LOCK은 바뀌지 않습니다. "
               "synthetic protocol only — qualification UNQUALIFIED · "
               "acceptance PENDING · release NOT_AUTHORIZED")
    board = _guarded(lambda: workbench(p))
    if board is None:
        return
    cols = st.columns(5)
    cols[0].metric("cue", board["counts"]["cues"])
    cols[1].metric("가사 검수", _state(board["review"]["state"]))
    cols[2].metric("미해결 행", board["counts"]["unresolved_rows"])
    cols[3].metric("긴 줄", board["counts"]["long_lines"])
    cols[4].metric("폰트", _state(board["font"]["report"]["status"]))
    for warning in board["warnings"]:
        st.warning(warning)
    src = board["source"]
    st.caption(f"원문 `input/lyrics.txt` {src['sha256'][:12]}… · "
               f"행 {board['rows']['lyric']}개 · 구절 표시 "
               f"{board['rows']['section']}개 · 반복 표시 "
               f"{board['rows']['repeat']}개 · timing "
               f"{board['timing_sha256'][:12]}…")

    _position_section(p, board)
    st.markdown("**cue 목록 — 프레임 경계와 읽기 지표**")
    _cue_table(board)
    _seam_table(board)
    _repeat_section(board)
    gaps = board["coverage_gaps"]
    if gaps:
        st.markdown("**누락 — 타이밍되지 않은 원문 구간**")
        st.dataframe(
            [{"행": g["row_id"], "구간": f"{g['span'][0]}–{g['span'][1]}자",
              "텍스트": g["text"]} for g in gaps], hide_index=True)
    _font_section(board)
    _diff_section(board)
    _builds_section(board)
    _invalidation_section(p, board)
    st.markdown("---")
    _candidate_section(p)
