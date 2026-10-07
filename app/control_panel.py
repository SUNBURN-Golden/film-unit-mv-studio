from pathlib import Path
import json
import os
import sys
import tempfile
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st
from engine.core import FilmError, atomic_text, lock_production, production_fingerprint, production_profile, project_mutex, read, write
from engine.brief import create_project
from engine.audio import analyze
from engine.production import generate_storyboard, import_frame, make_package
from engine.pipeline import compile_project, prepare
from engine.budget import approve
from engine.qc import SEMANTIC_ITEMS, save_review
from engine.settings import get_secret

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = Path(os.environ.get("FILM_UNIT_PROJECTS", ROOT / "projects")).resolve()
st.set_page_config(page_title="FILM UNIT", page_icon="◧", layout="wide")
st.markdown("""<style>
.stApp{background:#eeeae3;color:#20221f} h1,h2,h3{letter-spacing:-.035em}
[data-testid="stSidebar"]{background:#e1ddd4}.stButton>button{border-radius:3px}
[data-testid="stMetric"]{border-top:1px solid #b6b3aa;padding-top:12px}
</style>""", unsafe_allow_html=True)
st.caption("FILM UNIT   /   ASTRA MV COMPILER   /   0.3")
st.title("한 곡에서, 한 편으로.")
st.write("곡 전체를 먼저 보고, 장면과 가사 자막을 고쳐 새 빌드로 저장합니다.")
PROJECTS.mkdir(parents=True, exist_ok=True)
projects = sorted(p.name for p in PROJECTS.iterdir() if p.is_dir() and (p / "project.yaml").exists())
st.sidebar.caption(f"프로젝트 저장 위치: {PROJECTS}")
chosen = st.sidebar.selectbox("프로젝트", ["새 프로젝트", *projects], index=(len(projects) if projects else 0))


def guarded(fn, success=None):
    try:
        result = fn()
        if success:
            st.success(success)
        return result
    except Exception as e:
        st.error(str(e))
        return None


if chosen == "새 프로젝트":
    st.subheader("PROJECT")
    with st.form("new_project"):
        name = st.text_input("프로젝트 ID", "project_001")
        audio = st.file_uploader("Suno MP3 / WAV", type=["mp3", "wav"])
        brief = st.text_area("어떤 영상으로 만들까요?", placeholder="그림체, 인물, 이야기, 반복되는 이미지 등을 자유롭게 적어주세요.")
        lyrics = st.text_area("가사 원문", help="가사 자막의 원본입니다. 타이밍은 곡을 들으며 별도로 검토합니다.")
        emotion = st.text_input("원하는 감정·분위기", help="미확정이면 비워두세요. 제작 준비 탭에서 다시 정리할 수 있습니다.")
        aspect = st.selectbox("화면비", ["16:9", "4:3", "9:16"], help="Gemini Veo 자동 제작은 16:9 또는 9:16만 지원합니다.")
        submit = st.form_submit_button("프로젝트 만들기", type="primary")
    if submit:
        if not audio or not brief.strip():
            st.error("음원과 짧은 설명을 넣어주세요.")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / ("master" + Path(audio.name).suffix.lower())
                source.write_bytes(audio.getvalue())
                result = guarded(lambda: create_project(PROJECTS, name, source, brief, lyrics, emotion=emotion, aspect=aspect))
                if result:
                    from desktop.runtime import configure_project_font
                    configure_project_font(result)
                    st.success("프로젝트가 생성됐습니다. 왼쪽에서 선택해주세요.")
    st.stop()

p = PROJECTS / chosen
config = read(p / "project.yaml")
audio_path = p / config["audio"]["path"]
if config["audio"].get("synthetic_test_audio"):
    st.info("파이프라인 검증용 합성 음원입니다. 콘티 그림은 배치 확인용 도식입니다.")
if audio_path.is_file():
    st.sidebar.audio(str(audio_path))
tab_names = ["00 · AI 모델", "00 · 웹사이트 연결", "01 · 제작 준비", "01 · PROJECT", "02 · DIRECTOR", "03 · STORYBOARD", "04 · LYRICS", "05 · TIMELINE", "06 · COMPILE / BUILDS", "07 · RENDER · 고급"]
if production_profile(config) == "FRAME_ANIMATION_V1":
    tab_names.append("08 · ANIMATION")
    tab_names.append("09 · 자산")
    tab_names.append("10 · 컷 작업대")
models_tab, web_tab, brief_tab, *tabs = st.tabs(tab_names)

with models_tab:
    from app.models_ui import render_picker
    render_picker(p)

with web_tab:
    from app.handoff_ui import render_connection
    render_connection(p)
    from app.archive_ui import render_archive
    render_archive(p)
    from app.capability_ui import render_capabilities, render_forecast
    render_capabilities(p)
    render_forecast(p)

with brief_tab:
    from app.brief_board import render_brief_board
    render_brief_board(p)

with tabs[0]:
    st.subheader("음원 분석")
    st.write((p / "input/brief.md").read_text(encoding="utf-8"))
    if st.button("ANALYZE", type="primary"):
        with st.spinner("음원의 비트와 에너지 변화를 분석하고 있습니다…"):
            guarded(lambda: analyze(p), "분석 완료")
    if (p / "analysis/audio.json").exists():
        data = read(p / "analysis/audio.json")
        cols = st.columns(3)
        cols[0].metric("길이", f"{data['duration_ms']/1000:.3f}s")
        cols[1].metric("추정 BPM", f"{data['tempo_bpm']:.1f}" if data.get("tempo_bpm") else "미검출")
        cols[2].metric("검출 비트", len(data.get("beat_times_ms", [])))
        if (p / "analysis/waveform.png").exists():
            st.image(str(p / "analysis/waveform.png"))
        st.caption("섹션 경계는 음향 변화 후보입니다. 벌스·후렴 명칭은 감독 검토 후 지정합니다.")

with tabs[1]:
    st.subheader("제작 패키지")
    st.write("실제 비트에 맞춘 샷과 편집 가능한 제작 문서를 준비합니다. 초기 콘티는 배치 도식이며, 실제 연출과 이미지는 Work에서 완성해 넣습니다.")
    preset_labels = {"neutral": "중립 · 새 작품", "water_please": "물 좀 주소 · 기존 작품 preset", "concrete_glide": "Concrete Glide · 군청 도시 애니마틱"}
    preset = st.selectbox("시작 템플릿", list(preset_labels), format_func=preset_labels.get)
    if st.button("GENERATE PRODUCTION PACKAGE", disabled=not (p / "analysis/audio.json").exists()):
        guarded(lambda: make_package(p, preset=preset), "제작 패키지 생성 완료")
    bible_files = ["story.md", "style_bible.yaml", "characters.yaml", "locations.yaml", "directing.yaml"]
    for name in bible_files:
        path = p / "bible" / name
        if path.exists():
            with st.expander(name):
                content = st.text_area("내용", path.read_text(encoding="utf-8"), height=260, key=name)
                if st.button("저장", key="save_"+name):
                    def save_bible():
                        with project_mutex(p):
                            if path.suffix == ".yaml":
                                value = yaml.safe_load(content)
                                if not isinstance(value, dict):
                                    raise FilmError("YAML must contain an object")
                                write(path, value)
                            else:
                                atomic_text(path, content)
                    guarded(save_bible, "저장했습니다. 변경된 제작 패키지는 다시 LOCK해주세요.")
    if (p / "bible/director_request.md").exists():
        st.download_button("Work 감독 작업 지시서", (p / "bible/director_request.md").read_text(encoding="utf-8"), "director_request.md")
    st.divider()
    from app.models_ui import render_director
    render_director(p)

with tabs[2]:
    st.subheader("콘티 검토")
    if (p / "manifest/shots.json").exists():
        shots = read(p / "manifest/shots.json")
        locked = read(p / "manifest/locks.json", {})
        try:
            is_locked = locked.get("fingerprint") == production_fingerprint(p)
        except Exception:
            is_locked = False
        st.metric("STORYBOARD", f"{len(shots) if is_locked else 0} / {len(shots)} LOCKED")
        for row in range(0, len(shots), 3):
            for col, shot in zip(st.columns(3), shots[row:row+3]):
                with col:
                    references = shot.get("references", [])
                    if references and (p / references[0]).is_file():
                        st.image(str(p / references[0]))
                    else:
                        st.info("첫 프레임 없음 · Preview에서는 임시 화면을 사용합니다.")
                    st.caption(f"{shot['id']} · {shot['in_ms']/1000:.3f}–{shot['out_ms']/1000:.3f}s · {shot['render_mode']}")
                    with st.expander("EDIT / REPLACE", expanded=False):
                        desc = st.text_area("장면", shot["description"], key="desc_"+shot["id"])
                        instruction = st.text_area("움직임", shot.get("motion", {}).get("instruction", ""), key="motion_"+shot["id"])
                        kind = st.selectbox("렌더 방식", ["STATIC", "LIMITED_MOTION", "FULL_GENERATIVE"], index=["STATIC", "LIMITED_MOTION", "FULL_GENERATIVE"].index(shot["render_mode"]), key="mode_"+shot["id"])
                        if st.button("연출 저장", key="edit_"+shot["id"]):
                            def save_direction():
                                with project_mutex(p):
                                    current = read(p / "manifest/shots.json")
                                    target = next(s for s in current if s["id"] == shot["id"])
                                    target.update(description=desc, render_mode=kind)
                                    target.setdefault("motion", {})["instruction"] = instruction
                                    write(p / "manifest/shots.json", current)
                                    generate_storyboard(p)
                                return True
                            if guarded(save_direction):
                                st.rerun()
                        uploaded = st.file_uploader("승인할 첫 프레임", type=["png", "jpg", "jpeg"], key="frame_"+shot["id"])
                        if uploaded and st.button("이미지 교체", key="replace_"+shot["id"]):
                            guarded(lambda: import_frame(p, shot["id"], uploaded), "교체 완료; 다시 LOCK해주세요.")
                            st.rerun()
                        if shot.get("storyboard_kind") == "placeholder" and st.button("도식 다시 만들기", key="regen_"+shot["id"]):
                            generate_storyboard(p, shot["id"])
                            st.rerun()
        st.divider()
        if st.button("구독 앱 작업지시서 만들기"):
            from engine.packets import export_packets
            made = guarded(lambda: export_packets(p))
            if made:
                st.success(f"{made['shots']}개 샷 작업지시서: {made['packets']}")
                for warning in made["warnings"]:
                    st.warning(warning)
        reviewer = st.text_input("검토자", "Director")
        mock_only = st.checkbox("콘티 테스트용 LOCK", value=any(s.get("storyboard_kind") == "placeholder" for s in shots))
        if st.button("LOCK ALL", type="primary"):
            result = guarded(lambda: (lock_production(p, reviewer, mock_only), True)[1], "전체 제작 패키지를 LOCK했습니다.")
            if result:
                st.rerun()

with tabs[6]:
    with st.expander("자동 제작 · 고른 모델로 참조 이미지 → 첫 프레임 → 영상 → Preview", expanded=True):
        from app.models_ui import render_autopilot
        render_autopilot(p, "autopilot_" + chosen)
    st.subheader("샷 생성 · 고급")
    st.caption("기존 렌더러로 개별 장면을 생성하거나 이어서 실행합니다. 전체 영상을 보려면 COMPILE / BUILDS의 Preview를 사용하세요.")
    renderer = st.selectbox("Renderer", ["mock", "economy", "manual", "openart", "fal", "gemini"], format_func=lambda x: {"mock":"Mock · 비용 없는 콘티 테스트", "economy":"Economy · 실험 기능", "manual":"Manual · 생성한 영상 가져오기", "openart":"OpenArt · Work 작업 연결", "fal":"fal · 실험 기능", "gemini":"Gemini · Veo 3.1 Lite"}[x])
    quality = st.radio("Quality", ["draft", "final"], index=1, horizontal=True)
    length = st.radio("길이", ["30초 테스트", "60초 Pilot", "곡 전체"], horizontal=True)
    seconds = {"30초 테스트": 30, "60초 Pilot": 60, "곡 전체": None}[length]
    budget_cap = st.number_input("이 프로젝트의 OpenArt 크레딧 한도", min_value=0, value=int(config["budget"]["max_credits"]))
    usd_cap = st.number_input("이 프로젝트의 달러 예산 (fal · Gemini, USD)", min_value=0.0, value=float(config["budget"].get("max_usd", 0)), step=0.1, format="%.2f")
    retry_cap = st.number_input("샷당 최대 재시도", min_value=0, max_value=10, value=int(config["budget"]["max_retry_per_shot"]))
    if st.button("예산 설정 저장"):
        config["budget"].update(max_credits=budget_cap, max_usd=usd_cap, max_retry_per_shot=retry_cap)
        write(p / "project.yaml", config)
        st.success("저장 완료")
    if st.button("예상 비용 확인"):
        prepared = guarded(lambda: prepare(p, seconds, renderer, quality))
        if prepared:
            st.session_state["estimate_"+chosen] = prepared[4]
    estimate = st.session_state.get("estimate_"+chosen)
    if estimate:
        unit = estimate.get("billing_unit", "credits")
        pools = estimate["pools"] if unit == "mixed" else {unit: estimate}
        for pool_unit, pool in pools.items():
            cols = st.columns(3)
            cols[0].metric("첫 생성", f"{pool['initial_amount']:g} {pool_unit}")
            cols[1].metric("재시도 여유분", f"{pool['retry_reserve']:g} {pool_unit}")
            cols[2].metric("최대 예상", f"{pool['worst_case_amount']:g} {pool_unit}")
        if unit == "mixed":
            st.caption("달러와 크레딧은 별도 한도입니다. 아래 상한에는 이미 보관한 영상도 포함되며, 같은 원본 재사용에는 생성비가 들지 않습니다.")
            routes = [{"장면": row["shot"], "시도": a["attempt"] + 1, "설정": a["profile"], "도구": a["provider"], "금액": a["amount"], "단위": a["billing_unit"]} for row in estimate["rows"] for a in row["attempts"]]
            st.dataframe(routes, hide_index=True, width="stretch")
        if any(pool["worst_case_amount"] > 0 for pool in pools.values()) and st.button("이 배치 비용 승인"):
            guarded(lambda: approve(p, estimate), "승인 완료")
    if renderer == "economy":
        st.caption("검증된 후보 중 도구 우선순위와 가격에 따라 선택합니다. 실패한 장면만 승인된 순서로 재시도하고, 검토를 기다리는 동안 추가 생성하지 않습니다.")
        if not (p / "render/economy.json").exists() and st.button("연결된 설정으로 Economy 준비"):
            from engine.economy import initialize
            guarded(lambda: initialize(p), "설정 파일을 만들었습니다. Work에서 후보 모델과 견적을 연결해주세요.")
    if renderer == "fal":
        st.caption("Wan 2.2 Turbo · 최대 5초 샷 · 기본 720p. 1080p 출력은 편집 시 확대됩니다.")
        if not get_secret("FAL_KEY"):
            st.info("fal API 키가 아직 연결되지 않았습니다. '00 · AI 모델'에서 입력하거나 FAL_KEY를 설정해주세요.")
        if not (p / "render/fal_config.json").exists():
            if st.button("Wan 테스트 설정 추가"):
                write(p / "render/fal_config.json", read(ROOT / "templates/fal_wan_turbo.json"))
                st.success("설정 완료. 달러 예산을 저장하고 콘티 LOCK 후 비용을 확인해주세요.")
    if renderer == "gemini":
        st.caption("Veo 3.1 Lite · 16:9/9:16 프로젝트 · 최대 8초 샷 · 720p. 생성된 영상은 Google 서버에 2일만 보관되어 바로 내려받습니다.")
        if not (p / "render/gemini_config.json").exists() and st.button("Gemini 설정 추가"):
            from engine.gemini import DEFAULT_CONFIG
            write(p / "render/gemini_config.json", DEFAULT_CONFIG)
            st.success("설정 완료. 달러 예산을 저장하고 콘티 LOCK 후 비용을 확인해주세요.")
    if renderer == "openart":
        st.caption("Work에서 모델 규격·첫 프레임 URL·견적을 연결하면 승인된 요청 파일을 만듭니다. Work가 OpenArt에 제출한 후 결과를 가져와 이어서 실행합니다.")
    if st.button("GENERATE MUSIC VIDEO / RESUME", type="primary"):
        progress = st.progress(0, text="준비 중")
        result = guarded(lambda: compile_project(p, seconds, renderer, quality,
            progress=lambda i,n,s: progress.progress(i/max(n,1), text=f"{s} · {i}/{n}")))
        if result:
            st.session_state["last_result_"+chosen] = result
            st.success("출력 완료" if result["status"] == "COMPLETE" else "영상 업로드 또는 QC 검토 후 RESUME해주세요.")
    if (p / "qc/report.json").exists():
        from engine.work_queue import status as work_status
        with st.expander("작업 대기 목록과 사용 한도"):
            st.json(work_status(p))
        report = read(p / "qc/report.json")
        for record in report["shots"]:
            if record["status"] == "AWAITING_RENDER" and renderer == "manual":
                st.write(record["message"])
                upload = st.file_uploader(record["shot_id"]+" 영상", type=["mp4"], key="clip_"+record["shot_id"])
                if upload and st.button("영상 가져오기", key="import_"+record["shot_id"]):
                    (p / f"render/manual/{record['shot_id']}_a{record['attempt']}.mp4").write_bytes(upload.getvalue())
                    st.success("가져왔습니다. RESUME을 눌러주세요.")
            if record["status"] == "NEEDS_REVIEW":
                with st.expander(record["shot_id"] + " · QC 검토", expanded=True):
                    st.video(str(p / record["clip_path"]))
                    for col, frame in zip(st.columns(5), record.get("frame_samples", [])):
                        col.image(str(p / frame["path"]))
                    st.caption("직접 확인한 항목만 점수를 입력하세요. 빈 항목은 통과로 처리되지 않습니다.")
                    scores = {k: st.number_input(k, min_value=0, max_value=100, value=None, key=f"{record['clip_sha256']}_{k}") for k in SEMANTIC_ITEMS}
                    notes = st.text_area("검사 근거와 필요한 수정", key="notes_"+record["clip_sha256"])
                    person = st.text_input("QC 검토자", key="reviewer_"+record["clip_sha256"])
                    if st.button("QC 기록 저장", key="qc_"+record["clip_sha256"]):
                        guarded(lambda: save_review(p, record, scores, person, notes), "저장했습니다. RESUME하면 기준 미달 샷은 수정사항과 함께 재시도됩니다.")
                    if record.get("raw_path"):
                        st.caption("원본에 더 좋은 구간이 있으면 새 영상 생성 없이 시작점을 바꿀 수 있습니다.")
                        offset = st.number_input("원본 시작점 (ms)", min_value=0, value=int(record.get("source_in_ms", 0)), key="trim_"+record["clip_sha256"])
                        if st.button("이 구간으로 편집", key="trim_save_"+record["clip_sha256"]):
                            from engine.takes import select_window
                            def edit_window():
                                with project_mutex(p):
                                    return select_window(p, record["shot_id"], offset, notes)
                            guarded(edit_window, "저장했습니다. RESUME 후 편집된 영상을 검토해주세요.")

with tabs[3]:
    from engine.lyrics import prepare_lyrics, save_timing, validate_lyrics
    st.subheader("가사와 자막")
    st.write("원문을 보존하고, 실제 보컬에 맞춘 밀리초 타이밍을 검토합니다. 영상 컷을 바꿔도 가사 타이밍은 움직이지 않습니다.")
    with st.expander("자막 글꼴 설정"):
        st.caption("한글은 Noto Sans CJK KR 등 한글을 포함한 글꼴을 선택하세요. Final은 실제 글자 지원 여부를 검사합니다.")
        font_name = st.text_input("글꼴 이름", config.get("subtitles", {}).get("font_name", "DejaVu Sans"))
        font_file = st.text_input("글꼴 파일 · 프로젝트 내부 경로 (선택)", config.get("subtitles", {}).get("font_file", ""))
        if st.button("자막 글꼴 저장"):
            def save_font():
                with project_mutex(p):
                    current = read(p / "project.yaml")
                    current.setdefault("subtitles", {})["font_name"] = font_name
                    if font_file.strip():
                        from engine.core import safe_path
                        if not safe_path(p, font_file).is_file():
                            raise FilmError("글꼴 파일을 확인해주세요.")
                        current["subtitles"]["font_file"] = font_file.strip()
                    else:
                        current["subtitles"].pop("font_file", None)
                    write(p / "project.yaml", current)
            guarded(save_font, "저장했습니다. Final 전에 가사 검수와 LOCK을 다시 진행해주세요. 영상 검수는 유지됩니다.")
    source_path = p / "input/lyrics.txt"
    source = st.text_area("가사 원문", source_path.read_text(encoding="utf-8") if source_path.exists() else "", height=250, key="lyrics_source_" + chosen)
    if st.button("원문 저장 / 자막 문서 준비"):
        def save_source():
            with project_mutex(p):
                atomic_text(source_path, source)
                return prepare_lyrics(p)
        if guarded(save_source, "저장했습니다. 변경된 원문의 기존 타이밍은 보관하고 다시 검토합니다.") is not None:
            st.rerun()
    timed_path = p / "lyrics/lyrics_timed.json"
    if timed_path.exists():
        document = read(timed_path)
        st.dataframe(document.get("cues", []), hide_index=True, width="stretch")
        if (p / "analysis/audio.json").exists():
            def validate_timing():
                with project_mutex(p):
                    return validate_lyrics(p, read(p / "analysis/audio.json")["duration_ms"])
            validation = guarded(validate_timing)
            if validation:
                _, warnings = validation
                if warnings:
                    for warning in warnings:
                        st.warning(str(warning))
                else:
                    st.success("현재 자막 문서의 시간 범위와 원문 연결을 확인했습니다.")
        with st.expander("타이밍 JSON 편집 / 가져오기", expanded=not document.get("cues")):
            st.caption("cues의 source_row_id는 rows의 ID를 사용합니다. start_ms/end_ms는 원음 기준이며 시간을 균등 분배해 만들지 않습니다. 반복 표시는 실제 반복할 원문 행을 먼저 지정하세요.")
            timing_upload = st.file_uploader("검토할 lyrics_timed.json", type=["json"], key="timing_upload")
            timing_text = st.text_area("타이밍 문서", json.dumps(document, ensure_ascii=False, indent=2), height=330, key="timing_json_" + chosen + "_" + str(document.get("source_sha256", "")))
            reviewer = st.text_input("자막 검토자", key="lyrics_reviewer")
            reviewed = st.checkbox("실제 보컬과 구절의 시작·끝, 누락·반복 및 자막 표시 설정을 확인했습니다.", key="lyrics_reviewed")
            if st.button("타이밍 저장"):
                def import_timing():
                    content = timing_upload.getvalue().decode("utf-8") if timing_upload else timing_text
                    if reviewed and not reviewer.strip():
                        raise FilmError("검토자 이름을 입력해주세요.")
                    with project_mutex(p):
                        return save_timing(p, json.loads(content), reviewer=reviewer if reviewed else "")
                if guarded(import_timing, "자막 타이밍을 저장했습니다.") is not None:
                    st.rerun()
        st.download_button("타이밍 JSON 내보내기", timed_path.read_bytes(), "lyrics_timed.json", "application/json")
    else:
        st.info("원문 저장 / 자막 문서 준비를 누르면 검토할 원문 행과 빈 타이밍 문서를 만듭니다.")

with tabs[4]:
    from engine.timeline import merge_shots, move_cut, snap_cut, split_shot
    st.subheader("영상 타임라인")
    st.caption("수정한 샷의 영상 선택과 LOCK은 다시 검토합니다. 이미 저장한 빌드와 가사 타이밍은 유지됩니다.")
    if (p / "manifest/shots.json").exists():
        timeline_shots = read(p / "manifest/shots.json")
        st.dataframe([{k: s[k] for k in ["id", "sequence", "in_ms", "out_ms", "duration_ms", "description"]} for s in timeline_shots], hide_index=True, width="stretch")
        selected_id = st.selectbox("편집할 샷", [s["id"] for s in timeline_shots], key="timeline_shot")
        selected_index = next(i for i, s in enumerate(timeline_shots) if s["id"] == selected_id)
        selected_shot = timeline_shots[selected_index]
        split_at = st.number_input("분할 위치 (원음 기준 ms)", min_value=0, value=(selected_shot["in_ms"] + selected_shot["out_ms"]) // 2, step=100, key="split_ms_" + selected_id + "_" + str(selected_shot.get("visual_revision", 0)))
        if st.button("SPLIT SHOT"):
            if guarded(lambda: split_shot(p, selected_id, int(split_at)), "분할 완료"):
                st.rerun()
        if selected_index + 1 < len(timeline_shots):
            next_shot = timeline_shots[selected_index + 1]
            cut_at = st.number_input("다음 샷과의 컷 (원음 기준 ms)", min_value=0, value=selected_shot["out_ms"], step=100, key="cut_ms_" + selected_id + "_" + str(selected_shot.get("visual_revision", 0)))
            if st.button("MOVE CUT"):
                if guarded(lambda: move_cut(p, selected_id, int(cut_at)), "컷 이동 완료"):
                    st.rerun()
            for col, offset in zip(st.columns(4), [-500, -100, 100, 500]):
                if col.button(f"{offset:+d} ms", key="offset_" + str(offset)):
                    if guarded(lambda: move_cut(p, selected_id, selected_shot["out_ms"] + offset), "컷 이동 완료"):
                        st.rerun()
            col_beat, col_onset, col_merge = st.columns(3)
            if col_beat.button("SNAP TO BEAT"):
                if guarded(lambda: snap_cut(p, selected_id, "beat", int(cut_at)), "비트에 맞췄습니다."):
                    st.rerun()
            if col_onset.button("SNAP TO ONSET"):
                if guarded(lambda: snap_cut(p, selected_id, "onset", int(cut_at)), "온셋에 맞췄습니다."):
                    st.rerun()
            if col_merge.button("MERGE NEXT", disabled=selected_shot["sequence"] != next_shot["sequence"]):
                if guarded(lambda: merge_shots(p, selected_id, next_shot["id"]), "병합 완료; 앞 샷의 연출과 첫 프레임을 유지합니다."):
                    st.rerun()
        else:
            st.caption("마지막 샷의 끝은 원곡 길이에 고정됩니다.")

with tabs[5]:
    from engine.compiler import compile_final, compile_preview, import_asset
    from engine.builds import list_builds, verify_build
    st.subheader("곡 전체 컴파일")
    st.write("Preview는 final → draft → storyboard → 임시 화면 순으로 모든 샷을 연결합니다. Final은 LOCK, 최종 영상 검수, 가사 검토 조건을 통과해야 저장됩니다.")
    st.caption("각 실행은 B0001, B0002처럼 새 빌드로 보관됩니다. 이 버튼은 유료 영상 생성을 요청하지 않습니다.")
    compile_quality = st.radio("출력 화질", ["draft", "final"], horizontal=True, key="compile_quality")
    ready = (p / "analysis/audio.json").exists() and (p / "manifest/shots.json").exists()
    col_preview, col_final = st.columns(2)
    start_preview = col_preview.button("COMPILE PREVIEW · 곡 전체", type="primary", disabled=not ready)
    start_final = col_final.button("COMPILE FINAL · 검수본", disabled=not ready)
    if start_preview or start_final:
        progress = st.progress(0, text="곡 전체 빌드 준비 중")
        compile_fn = compile_preview if start_preview else compile_final
        result = guarded(lambda: compile_fn(p, quality=compile_quality,
            progress=lambda i, n, label: progress.progress(min(1.0, i / max(n, 1)), text=f"{label} · {i}/{n}")))
        if result:
            st.session_state["last_build_" + chosen] = result
            st.success(f"{result['build_id']} 저장 완료")
    if not ready:
        st.info("음원 분석과 제작 패키지를 먼저 준비해주세요.")
    if ready:
        with st.expander("생성하거나 편집한 샷 영상 가져오기"):
            import_shots = read(p / "manifest/shots.json")
            asset_shot = st.selectbox("대상 샷", [s["id"] for s in import_shots], key="asset_shot")
            asset_upload = st.file_uploader("샷 영상", type=["mp4", "mov", "webm", "mkv"], key="asset_upload")
            asset_kind = st.radio("용도", ["draft", "final"], horizontal=True, key="asset_kind")
            asset_offset = st.number_input("원본 영상 사용 시작점 (ms)", min_value=0, value=0, step=100, key="asset_offset")
            asset_reviewer = st.text_input("최종 영상 검토자", key="asset_reviewer")
            asset_evidence = st.text_area("검수 근거", key="asset_evidence", placeholder="인물, 스타일, 구도, 카메라, 움직임, 소품, 불필요한 글자 등을 직접 확인한 결과")
            asset_reviewed = st.checkbox("선택한 최종 영상 전체를 현재 제작 기준과 대조해 검수했습니다.", key="asset_reviewed")
            if st.button("샷 영상 보관 / 선택", disabled=asset_upload is None):
                def save_asset():
                    if asset_reviewed and (not asset_reviewer.strip() or not asset_evidence.strip()):
                        raise FilmError("최종 검수에는 검토자와 근거가 필요합니다.")
                    with tempfile.TemporaryDirectory() as tmp:
                        source_file = Path(tmp) / ("clip" + Path(asset_upload.name).suffix.lower())
                        source_file.write_bytes(asset_upload.getvalue())
                        return import_asset(p, asset_shot, source_file, kind=asset_kind,
                            reviewer=asset_reviewer if asset_reviewed else "",
                            evidence=asset_evidence if asset_reviewed else "",
                            source_in_ms=int(asset_offset))
                guarded(save_asset, "샷을 보관했습니다. 새 Preview에서 확인해주세요.")
    st.divider()
    st.subheader("보관한 빌드")
    builds = list_builds(p)
    if builds:
        selection = st.selectbox("빌드", builds, format_func=lambda b: f"{b['build_id']} · {b.get('mode', '')} · {b.get('status', '')}")
        build_dir = Path(selection["build_dir"])
        outputs = [build_dir / name for name in ["MASTER_SUBBED.mp4", "MASTER_CLEAN.mp4"] if (build_dir / name).exists()]
        if outputs:
            selected = st.selectbox("영상", outputs, format_func=lambda f: f.name, key="build_output")
            st.video(str(selected))
            st.download_button("EXPORT MP4", selected.read_bytes(), selected.name, "video/mp4")
        for name in ["lyrics.ass", "lyrics.srt"]:
            subtitle = build_dir / name
            if subtitle.exists():
                st.download_button(name + " 내보내기", subtitle.read_bytes(), name)
        if st.button("빌드 파일 무결성 확인"):
            verified = guarded(lambda: verify_build(build_dir))
            if verified:
                st.json(verified)
        if (build_dir / "build.json").exists():
            with st.expander("이 빌드의 소스와 검증 기록"):
                st.json(read(build_dir / "build.json"))
    else:
        st.caption("첫 Preview를 컴파일하면 빌드 이력이 여기에 나타납니다.")
    legacy_outputs = sorted(f for f in (p / "output").glob("*.mp4") if ".pending." not in f.name)
    if legacy_outputs:
        with st.expander("이전 방식으로 만든 출력"):
            legacy = st.selectbox("이전 출력", legacy_outputs, format_func=lambda f: f.name)
            st.video(str(legacy))
            st.download_button("이전 MP4 내보내기", legacy.read_bytes(), legacy.name, "video/mp4")

if len(tabs) > 7:
    with tabs[7]:
        from app.animation_ui import render_animation
        render_animation(p)
if len(tabs) > 8:
    with tabs[8]:
        from app.asset_library import render_asset_library
        render_asset_library(p)
if len(tabs) > 9:
    with tabs[9]:
        from app.shot_board import render_shot_board
        render_shot_board(p)
