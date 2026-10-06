"""제작 준비 (production preparation) board — the "01 · 제작 준비" tab.

One place to gather the song, brief, original lyrics, references and desired
emotion when work starts. Uploaded materials land in ``brief/staging/`` as
임시 자료 (temporary): they never feed compile or any review/LOCK fingerprint
until an explicit 채택 (adopt) button copies them into the existing project
inputs and records the digest. The impact section shows which reviews, lyric
cues and the production LOCK go stale after an adopted original changes; it
never edits or inherits those records, and it never derives vocal timing from
lyric length. All widgets are standard labelled Streamlit controls so
keyboard navigation works; no custom JS.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.brief import (adopt_audio, adopt_reference, adopt_text,
                          board_status, discard_material, stage_material,
                          stage_note)
from engine.core import FilmError

STATE_KO = {"CURRENT": "유효", "STALE": "재검수 필요", "MISSING": "없음",
            "UNREVIEWED": "미검수", "UNVERIFIED": "확인 불가", "DRAFT": "초안"}


def _guarded(fn):
    """Run one engine call; a refusal becomes a message, not a traceback."""
    try:
        return fn()
    except FilmError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"처리하지 못했습니다: {exc}")
    return None


def _done(fn, message):
    """Run a mutating engine action; on success flash it and redraw."""
    if _guarded(fn) is not None:
        st.session_state["bb_flash"] = message
        st.rerun()


def render_brief_board(p):
    st.subheader("제작 준비")
    st.caption("곡·brief·가사 원문·레퍼런스·원하는 감정을 한곳에 모읍니다. 올린 자료는 채택하기 전까지 '임시'이며 컴파일·검수·LOCK에 사용되지 않습니다.")
    flash = st.session_state.pop("bb_flash", None)
    if flash:
        st.success(flash)
    status = board_status(p)
    audio = status["audio"]
    impact = status["impact"]
    limits = status["limits"]

    st.caption(f"지원 음원: MP3 / WAV · 1초–{limits['max_seconds']}초 · "
               f"검증 기준 {limits['baseline_seconds']}초 · {limits['fps']}fps")

    st.markdown("**채택 입력**")
    if audio["present"] and audio["matches_config"]:
        duration = f" · 측정 {audio['duration_ms']/1000:.3f}s" if audio.get("duration_ms") else ""
        slate = " · 합성 테스트 음원(임시 슬레이트)" if audio["synthetic"] else ""
        st.write(f"음원 `{audio['path']}` · sha256 `{audio['sha256'][:12]}…`{duration}{slate}")
    elif audio["present"]:
        st.error("음원 파일이 채택된 digest와 다릅니다. 원곡을 확인해주세요.")
    else:
        st.error("음원이 없습니다. 파일을 임시 자료로 올린 뒤 '원곡으로 채택'해주세요.")
    st.write(f"brief — {status['brief']['chars']}자" if status["brief"]["present"] else "brief — 없음")
    lyric_state = STATE_KO.get(status["lyrics"]["review_state"], status["lyrics"]["review_state"])
    st.write(f"가사 원문 — {status['lyrics']['chars']}자 · "
             f"{status['lyrics']['cues']}개 cue · 미해결 {status['lyrics']['unresolved']}행 · 검수 {lyric_state}")
    st.write(f"원하는 감정 — {status['emotion'] or '미확정'}")
    if status["references"]:
        for ref in status["references"]:
            if not ref["present"]:
                st.error(f"레퍼런스 파일이 없습니다: `{ref['path']}`")
            elif not ref["matches"]:
                st.error(f"레퍼런스가 채택된 digest와 다릅니다: `{ref['path']}`")
            else:
                st.write(f"레퍼런스 `{ref['path']}` · `{ref['sha256'][:12]}…`"
                         + (f" · {ref['note']}" if ref.get("note") else ""))
    else:
        st.caption("채택된 레퍼런스가 없습니다.")
    if status["placeholders"]:
        st.info(f"{len(status['placeholders'])}개 샷이 임시 슬레이트(placeholder)입니다. 실제 제작 그림이 아닙니다.")
    if status["pending"]:
        st.warning("미확정: " + ", ".join(status["pending"]))

    st.markdown("**변경 영향**")
    lock = impact["lock"]
    cols = st.columns(3)
    cols[0].metric("제작 LOCK", STATE_KO.get(lock["state"], lock["state"]))
    cols[1].metric("가사 검수", lyric_state)
    current = sum(1 for r in impact["visual_reviews"] if r["state"] == "CURRENT")
    cols[2].metric("영상 검수", f"{current} / {len(impact['visual_reviews'])} 유효")
    if lock["state"] == "CURRENT" and lock.get("mock_only"):
        st.caption("현재 LOCK은 콘티 테스트용입니다. Final을 승인하지 않습니다.")
    if impact["needs_review"]:
        for item in impact["needs_review"]:
            st.warning(f"재검수 필요 — {item['target']}: {item['reason']}")
    else:
        st.caption("재검수가 필요한 변경이 없습니다.")
    st.caption("원곡은 측정된 타임라인에 묶여 있습니다. 다른 곡으로 바꾸려면 새 프로젝트로 가져오세요. "
               "가사 원문을 바꾸면 기존 타이밍은 lyrics/history에 보관되고 검수·LOCK은 다시 진행합니다.")

    st.divider()
    st.markdown("**임시 자료 올리기**")
    st.caption("이미지·메모·후보 음원 등 — 채택 전까지 프로젝트 입력에 들어가지 않습니다.")
    uploads = st.file_uploader("자료 파일", accept_multiple_files=True, key="bb_stage_files",
                               help="여러 파일을 고를 수 있습니다. 외부 링크는 가져오지 않고 메모로 남깁니다.")
    note = st.text_input("자료 메모 (선택)", key="bb_stage_note")
    if st.button("임시 자료로 보관", key="bb_stage"):
        if not uploads:
            st.error("올릴 파일을 선택해주세요.")
        else:
            done = [f.name for f in uploads
                    if _guarded(lambda f=f: stage_material(p, f.name, f.getvalue(), note))]
            if done:
                st.session_state["bb_flash"] = f"{len(done)}개를 임시 자료로 보관했습니다."
                st.rerun()
    memo = st.text_area("레퍼런스 메모·링크", key="bb_memo",
                        placeholder="보고 싶은 그림, 링크, 메모 — 붙여넣기만 지원합니다. 앱이 외부에서 가져오지 않습니다.")
    if st.button("메모를 임시 자료로 보관", key="bb_memo_add"):
        _done(lambda: stage_note(p, memo), "메모를 임시 자료로 보관했습니다.")
    for record in status["staged"]:
        badge = "임시" if record["state"] == "temporary" else "채택됨"
        row = st.columns([6, 2, 2, 1])
        row[0].write(f"`{badge}` {record['name']} · {record['kind']} · `{record['sha256'][:10]}…`"
                     + (f" · {record['note']}" if record.get("note") else ""))
        if record["state"] != "temporary":
            continue
        if row[1].button("원곡으로 채택", key=f"bb_audio_{record['id']}"):
            _done(lambda r=record: adopt_audio(p, r["id"]), "원곡으로 채택했습니다.")
        if row[2].button("레퍼런스로 채택", key=f"bb_ref_{record['id']}"):
            _done(lambda r=record: adopt_reference(p, r["id"]), "레퍼런스로 채택했습니다.")
        if row[3].button("삭제", key=f"bb_del_{record['id']}"):
            _done(lambda r=record: discard_material(p, r["id"]), "임시 자료를 삭제했습니다.")

    st.divider()
    st.markdown("**기획 입력 채택**")
    brief_text = st.text_area("제작 brief", status["brief"]["text"], height=140,
                              key="bb_brief_text")
    if st.button("brief 채택", key="bb_adopt_brief"):
        _done(lambda: adopt_text(p, "brief", brief_text),
              "brief를 채택했습니다. 공통 입력이 바뀌면 영상 검수와 LOCK을 다시 확인하세요.")
    lyrics_text = st.text_area(
        "가사 원문", status["lyrics"]["text"], height=180, key="bb_lyrics_text",
        help="타이밍은 곡을 들으며 04 · LYRICS에서 별도 검토합니다. 글자 수나 곡 길이로 보컬 시점을 추정하지 않습니다.")
    if st.button("가사 원문 채택", key="bb_adopt_lyrics"):
        _done(lambda: adopt_text(p, "lyrics", lyrics_text),
              "가사 원문을 채택했습니다. 바뀐 원문의 기존 타이밍은 이력에 보관하고 다시 검토합니다.")
    emotion = st.text_input("원하는 감정·분위기", status["emotion"], key="bb_emotion")
    if st.button("감정 채택", key="bb_adopt_emotion"):
        _done(lambda: adopt_text(p, "emotion", emotion), "감정을 채택했습니다.")
