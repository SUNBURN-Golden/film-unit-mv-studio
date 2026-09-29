"""Browser hand-off panels: use subscription web apps by hand, with copy, paste and drag-and-drop.

The app never signs in to a web service or drives its page. Each service opens in the
User's own browser through a plain link; results come back by paste, upload, or a
one-time look at the downloads folder.
"""
import streamlit as st

from engine import handoff
from engine.core import FilmError

FLASH = "_handoff_flash"


def _flash_show():
    for kind, message in st.session_state.pop(FLASH, []):
        getattr(st, kind)(message)


def _flash(*items):
    st.session_state[FLASH] = list(items)


def _guarded(fn):
    try:
        return fn()
    except FilmError as exc:
        st.error(str(exc))
    except Exception as exc:                       # keep the panel usable whatever a file did
        st.error(f"처리하지 못했습니다: {exc}")
    return None


def service_picker(role, key):
    """Pick a web service and get a link that opens it in the User's own browser."""
    options = handoff.services_for(role)
    ids = [s.id for s in options] + ["custom"]
    names = {s.id: s.name for s in options} | {"custom": "다른 사이트"}
    pick = st.radio("열어 볼 웹사이트", ids, format_func=names.get, horizontal=True, key=key)
    if pick == "custom":
        url = st.text_input("웹사이트 주소 (https://…)", key=key + "_url")
        chosen = _guarded(lambda: handoff.custom_service(url)) if url.strip() else None
    else:
        chosen = handoff.service(pick)
    if chosen:
        st.link_button(f"{chosen.name} 열기 ↗", chosen.url, help="내 브라우저의 새 탭에서 열립니다. 로그인은 그 사이트에서 직접 합니다.")
        st.caption(chosen.note)
    return chosen


NOTICE = ("앱은 이 사이트에 로그인하거나 대신 조작하지 않습니다(ChatGPT·Claude·Grok 등의 약관이 자동 조작을 금지합니다). "
          "사이트는 내 브라우저에서 열고, 복사·붙여넣기와 파일 끌어놓기만 여기서 합니다.")


def render_text(p, notes, language):
    """Storyboard by copy and paste. The result joins the same review-and-accept step as the API drafts."""
    _flash_show()
    st.markdown("#### 웹사이트에서 콘티 만들기 (복사 · 붙여넣기)")
    st.caption(NOTICE)
    service = service_picker("text", "hand_text_service")
    state = _guarded(lambda: handoff.storyboard_state(p))
    if not state or service is None:
        return
    tag = f"{state['step']}_{state['done']}"
    if state["step"] == "world":
        st.markdown("**1단계 · 이야기, 화풍, 인물, 장소**")
        st.write("① 아래 지시문을 복사(코드 상자 오른쪽 위 버튼)해 위 사이트의 새 대화에 붙여넣으세요. ② 사이트의 답을 통째로 복사해 아래에 붙여넣으세요.")
        request = _guarded(lambda: handoff.world_request(p, notes, language))
        if request:
            with st.expander("복사할 지시문", expanded=True):
                st.code(request, language=None, wrap_lines=True)
        answer = st.text_area("사이트의 답을 여기에 붙여넣기", key=f"hand_answer_{tag}", height=220)
        if st.button("답 확인하고 받기", type="primary", key=f"hand_submit_{tag}", disabled=not answer.strip()):
            if _guarded(lambda: handoff.submit_world(p, answer, service.id, notes, language)):
                _flash(("success", "이야기와 인물, 장소를 받았습니다. 이어서 샷 연출을 받으세요."))
                st.rerun()
    elif state["step"] == "shots":
        nxt = _guarded(lambda: handoff.shots_request(p))
        if not nxt:
            return
        st.markdown(f"**2단계 · 샷 연출** · {state['done']}/{state['total']}개 받음 · 이번 차례: {nxt['ids'][0]} – {nxt['ids'][-1]}")
        st.write("같은 방법으로 아래 지시문을 새 대화에 붙여넣고 답을 붙여넣으세요. 샷이 많으면 이 과정이 여러 번 나옵니다.")
        with st.expander("복사할 지시문", expanded=True):
            st.code(nxt["text"], language=None, wrap_lines=True)
        answer = st.text_area("사이트의 답을 여기에 붙여넣기", key=f"hand_answer_{tag}", height=220)
        if st.button("답 확인하고 받기", type="primary", key=f"hand_submit_{tag}", disabled=not answer.strip()):
            after = _guarded(lambda: handoff.submit_shots(p, answer))
            if after:
                _flash(("success", f"{after['done']}/{after['total']}개 샷의 연출을 받았습니다."))
                st.rerun()
    else:
        st.success("모든 샷의 연출을 받았습니다. 아래에서 검토하고 제작 문서에 반영하세요.")


# ---- images and videos ---------------------------------------------------------------------

KIND_ROLE = {"reference": "image", "frame": "image", "video": "video"}
KIND_TYPES = {"reference": ["png", "jpg", "jpeg", "webp"], "frame": ["png", "jpg", "jpeg", "webp"],
              "video": ["mp4", "mov", "m4v", "webm", "mkv"]}
SKIP = "(넣지 않음)"


def _targets(p, kind):
    if kind == "reference":
        return [f"{m['folder']}/{m['id']}" for m in handoff.missing_references(p)]
    rows = handoff.shot_status(p)
    if kind == "frame":
        return [r["id"] for r in rows if not r["frame"]]
    return [r["id"] for r in rows if r["needs_video"] and not r["video"]]


def _incoming(kind, key):
    """Files the User dropped here, or found once in the downloads folder: [(name, size, function returning bytes)]."""
    files = []
    dropped = st.file_uploader("파일을 여기에 끌어다 놓기 (여러 개 가능)", type=KIND_TYPES[kind], accept_multiple_files=True, key=f"{key}_drop")
    files += [(f.name, f.size, f.getvalue) for f in dropped or []]
    role = KIND_ROLE[kind]
    with st.expander("다운로드 폴더에서 가져오기"):
        st.caption(f"웹사이트에서 내려받은 파일이 있는 폴더: `{handoff.downloads_folder()}` (`FILM_UNIT_DOWNLOADS`로 바꿀 수 있습니다). 버튼을 누를 때 한 번만 봅니다.")
        minutes = st.select_slider("최근 몇 분 안에 받은 파일", [15, 60, 180, 720, 1440], value=180, key=f"{key}_minutes")
        if st.button("다운로드 폴더 확인", key=f"{key}_scan"):
            st.session_state[f"{key}_found"] = handoff.scan_downloads(role, minutes=minutes)
        found = st.session_state.get(f"{key}_found")
        if found is not None and not found:
            st.info("최근 받은 파일이 없습니다.")
        if found:
            sizes = {f["name"]: f["size"] for f in found}
            names = st.multiselect("가져올 파일 (오래된 것부터 순서대로 배정됩니다)", [f["name"] for f in reversed(found)], key=f"{key}_names")
            # Only names are kept here; each file is read at the moment it is imported.
            files += [(name, sizes[name], (lambda n=name: handoff.read_download(n, role))) for name in names]
    return files


def _bulk(p, kind, key, service):
    targets = _targets(p, kind)
    if not targets:
        st.success("이 항목은 모두 채워졌습니다.")
        return
    files = _incoming(kind, key)
    if not files:
        return
    st.markdown("**어느 것에 넣을까요?** 순서대로 자동 배정했습니다. 바꿀 수 있습니다.")
    defaults = handoff.default_targets(len(files), targets)
    picks = []
    for index, ((name, size, _), default) in enumerate(zip(files, defaults)):
        options = [SKIP] + targets
        picks.append(st.selectbox(f"{name} ({size // 1024:,} KB)", options, index=options.index(default) if default else 0, key=f"{key}_pick_{index}_{name}"))
    chosen = [None if pick == SKIP else pick for pick in picks]
    if st.button(f"선택한 {sum(1 for c in chosen if c)}개 넣기", type="primary", key=f"{key}_go", disabled=not any(chosen)):
        results = _guarded(lambda: handoff.import_many(p, kind, [(t, n, load) for t, (n, _, load) in zip(chosen, files)], service.id if service else "custom"))
        if results is None:
            return
        items = []
        for result in results:
            if result["ok"]:
                items.append(("success", f"{result['file']} → {result['target']}"))
                items += [("warning", f"{result['target']}: {w}") for w in result["warnings"]]
            else:
                items.append(("error", f"{result['file']} → {result['target']}: {result['message']}"))
        items.append(("info", "가져온 뒤에는 콘티를 검토하고 다시 LOCK해야 합니다."))
        _flash(*items)
        st.rerun()


def _status_table(p):
    rows = handoff.shot_status(p)
    st.dataframe([{"샷": r["id"], "방식": r["render_mode"], "길이(초)": r["seconds"], "첫 프레임": "✅" if r["frame"] else "⬜",
                   "영상": ("✅" if r["video"] else "⬜") if r["needs_video"] else "정지"} for r in rows], hide_index=True)


def _one_prompt(p, kind, targets):
    """The prompt (and files to attach) for one chosen target."""
    if not targets:
        return
    pick = st.selectbox("프롬프트를 볼 대상", targets, key=f"brief_{kind}")
    if kind == "reference":
        folder, _, entry = pick.partition("/")
        brief = _guarded(lambda: handoff.reference_brief(p, folder, entry))
        if brief:
            st.code(brief["prompt"], language=None, wrap_lines=True)
    elif kind == "frame":
        brief = _guarded(lambda: handoff.frame_brief(p, pick))
        if brief:
            if brief["attach"]:
                st.write("사이트에 함께 올릴 참조 이미지: " + ", ".join(f"`{a}`" for a in brief["attach"]))
            st.code(brief["prompt"], language=None, wrap_lines=True)
            with st.expander("짧은 버전 (글자 수 제한이 있는 서비스용)"):
                st.code(brief["short_prompt"], language=None, wrap_lines=True)
    else:
        brief = _guarded(lambda: handoff.video_brief(p, pick))
        if brief:
            note = "" if brief["frame_ready"] else " · 먼저 첫 프레임을 가져오면 그 이미지로 영상을 만들 수 있습니다"
            st.write(f"시작 프레임 `{brief['start_frame']}` · 최소 **{brief['seconds']}초**{note}")
            st.code(brief["prompt"], language=None, wrap_lines=True)


def render_media(p, image_manual, video_manual):
    """Bring back what web services made: reference sheets, first frames and shot clips."""
    st.markdown("#### 웹사이트에서 만든 이미지·영상 가져오기")
    st.caption(NOTICE + " 가져온 영상은 초안(draft)으로만 들어가며, 검토와 LOCK 규칙은 그대로입니다.")
    _flash_show()
    kinds = []
    if image_manual:
        kinds += [("reference", "인물·장소 참조"), ("frame", "첫 프레임")]
    if video_manual:
        kinds.append(("video", "샷 영상"))
    if not kinds:
        return
    _status_table(p)
    for tab, (kind, label) in zip(st.tabs([label for _, label in kinds]), kinds):
        with tab:
            targets = _targets(p, kind)
            st.write(f"남은 것: {len(targets)}개" + (" · " + ", ".join(targets[:12]) + (" …" if len(targets) > 12 else "") if targets else ""))
            service = service_picker(KIND_ROLE[kind], f"hand_service_{kind}")
            _one_prompt(p, kind, targets)
            _bulk(p, kind, f"hand_{kind}", service)


def render_connection(p):
    """The '웹사이트 연결' tab: bring back media made in the User's own browser."""
    from engine import providers
    st.subheader("웹사이트 연결")
    st.write("구독 중인 웹사이트(Gemini·Flow·Grok·ChatGPT 등)에서 만든 이미지와 영상을 가져옵니다. "
             "콘티(글)는 '01 · PROJECT'의 AI 감독에서 '직접 붙여넣기'를 고르면 같은 방식으로 진행됩니다.")
    if not (p / "manifest/shots.json").exists():
        st.info("먼저 '01 · PROJECT'에서 제작 패키지를 만들어 샷 목록을 준비하세요.")
        return
    image, video = providers.chosen(p, "image"), providers.chosen(p, "video")
    image_manual = bool(image and image.kind == "manual")
    video_manual = bool(video and video.kind == "manual")
    everything = st.checkbox("고른 모델과 상관없이 가져오기", key="hand_everything",
                             help="API 모델을 골랐어도 웹사이트에서 만든 파일을 직접 넣고 싶을 때 켭니다.")
    if not (image_manual or video_manual or everything):
        st.info("이미지나 영상 모델로 '✋ 직접 만들기'를 고르면 여기서 파일을 가져올 수 있습니다. ('00 · AI 모델')")
        return
    render_media(p, image_manual or everything, video_manual or everything)
