"""Model picker, AI director and autopilot panels for the control panel.

Every stage (director text, reference images/frames, video) lists the available
providers as one radio choice with its cost, connection state and notes, so a
model is chosen with a few clicks and no command line.
"""
import streamlit as st

from engine import director, imagegen, providers, settings
from engine.autopilot import autopilot
from engine.budget import approve as approve_video
from engine.core import FilmError, read

ICONS = {"free_tier": "🆓", "paid": "💰", "local": "🖥️", "manual": "✋", "test": "🧪"}
READY_WORD = {"manual": "바로 사용", "test": "바로 사용"}


def guarded(fn, success=None):
    try:
        result = fn()
        if success:
            st.success(success)
        return result
    except Exception as exc:
        st.error(str(exc))
        return None


def choice_label(provider):
    state = providers.status(provider.id)
    word = READY_WORD.get(provider.cost) if state["configured"] else None
    word = word or ("연결 정보 입력됨" if state["configured"] else "입력 필요")
    return f"{ICONS[provider.cost]} {provider.label} · {word}"


def _provider_form(provider):
    state, current = providers.status(provider.id), providers.values(provider)
    models = st.session_state.get(f"models_{provider.id}", [])
    if not provider.fields:
        return
    with st.form(f"form_{provider.id}"):
        secrets, plain = {}, {}
        for field in provider.fields:
            if field.secret:
                source = state["keys"].get(field.key)
                hint = {"env": "환경변수로 설정되어 있습니다", "saved": "저장됨 · 바꾸려면 새로 입력"}.get(source, "")
                secrets[field.key] = st.text_input(f"{field.label} ({field.key})", type="password", placeholder=hint, help=field.help or None)
            elif field.choices or (field.key == "model" and models):
                options = list(field.choices or models)
                kept = settings.get_settings(provider.id).get(field.key)
                if kept and kept not in options:
                    options.insert(0, kept)          # a model the User saved stays choosable
                index = options.index(current[field.key]) if current.get(field.key) in options else 0
                plain[field.key] = st.selectbox(field.label, options, index=index)
            else:
                plain[field.key] = st.text_input(field.label, current.get(field.key, ""), help=field.help or None)
        saved = st.form_submit_button("저장")
    if saved:
        try:
            for name, value in secrets.items():
                if value.strip():
                    settings.set_secret(name, value)
            settings.set_settings(provider.id, plain)
        except FilmError as exc:
            st.error(str(exc))
        else:
            st.session_state["_flash"] = ("success", "저장했습니다.")
            st.rerun()


def _provider_panel(p, provider):
    st.markdown(f"**{provider.label}** · {providers.COSTS[provider.cost]}")
    st.write(provider.summary)
    for note in provider.notes:
        st.caption("• " + note)
    for warning in providers.fit_warnings(p, provider.id):
        st.warning(warning)
    if provider.docs:
        st.caption(f"[공식 문서]({provider.docs})")
    _provider_form(provider)
    if provider.fields:
        cols = st.columns(2)
        if cols[0].button("연결 테스트", key=f"test_{provider.id}"):
            with st.spinner("확인하는 중입니다…"):
                result = providers.check(provider.id)
            st.session_state[f"models_{provider.id}"] = result["models"]
            note = " 위 '모델'에서 고른 뒤 저장하세요." if result["models"] else ""
            st.session_state["_flash"] = ("success" if result["ok"] else "error", result["message"] + note)
            st.rerun()  # Redraw so the model list appears in the form above.
        saved_keys = [name for name in providers.secret_names(provider) if providers.status(provider.id)["keys"].get(name) == "saved"]
        if saved_keys and cols[1].button("저장된 키 지우기", key=f"clear_{provider.id}"):
            for name in saved_keys:
                settings.delete_secret(name)
            st.rerun()


def render_picker(p):
    st.subheader("AI 모델 선택")
    flash = st.session_state.pop("_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    st.caption("🆓 무료 한도 · 💰 유료(사용한 만큼) · 🖥️ 내 컴퓨터(무료) · ✋ 직접 만들기 · 🧪 테스트. "
               "고른 모델은 이 프로젝트에 저장됩니다. API 키는 이 컴퓨터의 사용자 설정 폴더에만 저장되고 프로젝트 폴더에는 들어가지 않습니다. "
               "구독(ChatGPT·Claude·Grok 등)은 대체로 API와 별도 과금입니다.")
    now = providers.selection(p)
    for stage, title in providers.STAGES.items():
        st.markdown(f"### {title}")
        options = providers.providers_for(stage)
        ids = [o.id for o in options]
        current = now.get(stage) if now.get(stage) in ids else None
        pick = st.radio(title, ids, index=ids.index(current) if current else None, key=f"pick_{stage}",
                        format_func=lambda i: choice_label(providers.get(i)), label_visibility="collapsed")
        if pick and pick != current:
            if guarded(lambda: providers.select(p, stage, pick)) is not None:
                st.rerun()
        if current:
            with st.container(border=True):
                _provider_panel(p, providers.get(current))
        else:
            st.info("이 단계에 쓸 모델을 위에서 고르세요.")


def render_director(p):
    st.subheader("AI 감독")
    st.caption("이야기, 화풍, 인물, 장소와 샷별 연출을 AI가 초안으로 써 줍니다. 아래에서 검토하고 반영을 눌러야 제작 문서가 바뀌며, "
               "컷 타이밍과 가사·자막 시간은 바꾸지 않습니다.")
    if not (p / "manifest/shots.json").exists():
        st.info("먼저 위에서 제작 패키지를 만들어 샷 목록을 준비하세요.")
        return
    choice = providers.chosen(p, "text")
    if choice is None:
        st.info("'00 · AI 모델'에서 콘티 감독 모델을 먼저 고르세요. 🆓 무료 모델도 있습니다.")
        return
    st.write(f"사용할 모델: {ICONS[choice.cost]} **{choice.label}**")
    notes = st.text_area("감독에게 요청할 내용 (선택)", key="director_notes",
                         placeholder="예: 슬프지만 담담하게. 주인공은 40대 여성. 배경은 1990년대 서울.")
    language = st.selectbox("프롬프트 언어", ["English", "한국어"], help="이미지·영상 모델은 영어에서 품질이 가장 좋습니다. 이야기 요약은 항상 한국어입니다.")
    pending = director.pending(p)
    cols = st.columns(3)

    def run(resume):
        holder = st.empty()
        with st.spinner("AI 감독이 작업 중입니다. 모델에 따라 몇 분 걸릴 수 있습니다."):
            return guarded(lambda: director.draft(p, providers.selected_text(p), notes, language, resume,
                                                  progress=lambda message: holder.info(message)))
    if choice.kind == "manual":
        from app.handoff_ui import render_text
        render_text(p, notes, language)
        if pending and st.button("초안 버리기", key="director_discard"):
            director.discard(p)
            st.rerun()
    else:
        if cols[0].button("초안 만들기", type="primary", key="director_run"):
            if run(False):
                st.rerun()
        if pending and not director.complete(p) and cols[1].button("이어서 만들기", key="director_resume"):
            if run(True):
                st.rerun()
        if pending and cols[2].button("초안 버리기", key="director_discard"):
            director.discard(p)
            st.rerun()
    pending = director.pending(p)
    if not pending:
        return
    st.markdown("#### 초안 검토")
    st.caption(f"{pending['provider']} · {pending['model']} · {pending['created_at']}")
    for warning in pending.get("warnings", []):
        st.warning(warning)
    world = pending["world"]
    st.write(world["story"])
    st.json({"화풍": world["style"]["visual_style"], "규칙": world["style"]["rules"]}, expanded=False)
    st.dataframe([{"ID": c["id"], "이름": c["name"], "외형": c["look"], "행동": c["behavior"]} for c in world["characters"]], hide_index=True)
    st.dataframe([{"ID": x["id"], "설명": x["description"]} for x in world["locations"]], hide_index=True)
    shots = {s["id"]: s for s in read(p / "manifest/shots.json")}
    st.dataframe([{"샷": sid, "시간": f"{shots[sid]['in_ms']/1000:.1f}–{shots[sid]['out_ms']/1000:.1f}s", "방식": row["render_mode"],
                   "장면": row["description"], "움직임": row["motion"]["instruction"]}
                  for sid, row in pending["shots"].items() if sid in shots], hide_index=True)
    if not director.complete(p):
        st.warning(f"{len(pending['shots'])}/{len(shots)}개 샷까지 만들었습니다. "
                   + ("위에서 다음 차례의 답을 붙여넣으세요." if choice.kind == "manual" else "'이어서 만들기'로 나머지를 만드세요."))
        return
    reviewer = st.text_input("검토자", "Director", key="director_reviewer")
    if st.button("검토했고, 제작 문서에 반영하기", type="primary", key="director_accept"):
        summary = guarded(lambda: director.accept(p, reviewer), "반영했습니다.")
        if summary:
            st.info(summary["note"])
            st.rerun()


STAGE_TEXT = {
    "NEEDS_MODEL_CHOICE": "모델을 먼저 고르세요", "NEEDS_IMAGE_APPROVAL": "이미지 비용 승인 필요",
    "IMAGES_FAILED": "일부 이미지를 만들지 못했습니다", "FRAMES_READY_FOR_REVIEW": "첫 프레임 완성 · 콘티 검토 후 LOCK",
    "NEEDS_MANUAL_FRAMES": "첫 프레임을 직접 만들어 넣어주세요", "NEEDS_LOCK": "콘티 검토 후 LOCK 필요 (03 · STORYBOARD)",
    "NEEDS_VIDEO_APPROVAL": "영상 비용 승인 필요", "NEEDS_MANUAL_VIDEO": "샷 영상을 직접 만들어 넣어주세요",
    "GENERATING": "영상 생성 중 · 몇 분 뒤 다시 누르세요", "PREVIEW_READY": "Preview 완성 · 샷 검토 후 Final",
}
KIND_TEXT = {"references": "인물·장소 참조 이미지", "frames": "첫 프레임"}


def render_autopilot(p, session_key):
    st.caption("누를 때마다 이미 승인된 작업만 진행하고, 다음에 사람이 결정할 단계에서 멈춥니다. 비용이 드는 단계는 금액을 확인하고 승인해야 시작됩니다. "
               "무료 모델은 승인 없이 바로 진행됩니다.")
    image, video = providers.chosen(p, "image"), providers.chosen(p, "video")
    st.write("이미지: " + (f"{ICONS[image.cost]} {image.label}" if image else "미선택") + "  ·  영상: " + (f"{ICONS[video.cost]} {video.label}" if video else "미선택")
             + "  (바꾸려면 '00 · AI 모델')")
    if st.button("다음 단계 실행", type="primary", key="autopilot_run"):
        with st.spinner("진행 중입니다. 이미지와 영상 생성은 몇 분 걸릴 수 있습니다."):
            st.session_state[session_key] = guarded(lambda: autopilot(p))
    step = st.session_state.get(session_key)
    if not step:
        return
    stage = step["stage"]
    st.markdown(f"**{STAGE_TEXT.get(stage, stage)}**")
    if stage == "NEEDS_MODEL_CHOICE":
        st.write("미선택: " + ", ".join(step["missing"]))
    if step.get("worst_case_usd") is not None:
        if stage == "NEEDS_IMAGE_APPROVAL":
            st.write(f"{KIND_TEXT[step['kind']]} {step['count']}장 · 모델: {step['provider']}")
        cols = st.columns(2)
        cols[0].metric("첫 생성", f"{step['initial_usd']:g} USD")
        cols[1].metric("재시도 포함 최대", f"{step['worst_case_usd']:g} USD")
    for warning in step.get("warnings", []) or []:
        st.warning(warning)
    if step.get("failed") and stage == "IMAGES_FAILED":
        st.write("실패: " + ", ".join(step["failed"]))
    if stage == "NEEDS_IMAGE_APPROVAL" and st.button("이 이미지 비용 승인", key="approve_images"):
        guarded(lambda: imagegen.approve(p, step["kind"], step["estimate_id"]), "승인 완료. 다음 단계 실행을 눌러주세요.")
    if stage == "NEEDS_VIDEO_APPROVAL" and st.button("이 영상 비용 승인", key="approve_video"):
        def approve_now():
            current = read(p / "render/estimate.json")
            if current["estimate_id"] != step["estimate_id"]:
                raise FilmError("견적이 바뀌었습니다. 다음 단계 실행을 다시 눌러주세요.")
            approve_video(p, current)
        guarded(approve_now, "승인 완료. 다음 단계 실행을 눌러주세요.")
    if stage in {"NEEDS_MANUAL_FRAMES", "NEEDS_MANUAL_VIDEO"}:
        st.write(f"작업지시서: `{step['packets']}`")
        st.info("웹사이트에서 만든 파일은 위쪽 '00 · 웹사이트 연결' 탭에서 끌어다 놓아 넣을 수 있습니다.")
    if stage == "PREVIEW_READY":
        st.write(f"Preview: `{step['build']}`")
        if step.get("needs_review"):
            st.caption("검토할 샷: " + ", ".join(step["needs_review"]) + " · 아래 QC 검토에서 확인해주세요.")
    st.caption(step.get("next", ""))
