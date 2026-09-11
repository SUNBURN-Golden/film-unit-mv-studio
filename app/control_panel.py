from pathlib import Path
import json
import sys
import tempfile
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st
from engine.core import FilmError, atomic_text, init_project, lock_production, production_fingerprint, read, write
from engine.audio import analyze
from engine.production import generate_storyboard, import_frame, make_package
from engine.pipeline import compile_project, prepare
from engine.budget import approve
from engine.qc import SEMANTIC_ITEMS, save_review

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "projects"
st.set_page_config(page_title="FILM UNIT", page_icon="◧", layout="wide")
st.markdown("""<style>
.stApp{background:#eeeae3;color:#20221f} h1,h2,h3{letter-spacing:-.035em}
[data-testid="stSidebar"]{background:#e1ddd4}.stButton>button{border-radius:3px}
[data-testid="stMetric"]{border-top:1px solid #b6b3aa;padding-top:12px}
</style>""", unsafe_allow_html=True)
st.caption("FILM UNIT   /   ASTRA MV COMPILER   /   0.1")
st.title("한 곡에서, 한 편으로.")
st.write("음원을 분석하고, 콘티를 확정한 뒤, 장면을 연결합니다.")
projects = sorted(p.name for p in PROJECTS.iterdir() if p.is_dir() and (p / "project.yaml").exists())
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
        lyrics = st.text_area("가사 · 선택")
        submit = st.form_submit_button("프로젝트 만들기", type="primary")
    if submit:
        if not audio or not brief.strip():
            st.error("음원과 짧은 설명을 넣어주세요.")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / ("master" + Path(audio.name).suffix.lower())
                source.write_bytes(audio.getvalue())
                result = guarded(lambda: init_project(PROJECTS, name, source, brief, lyrics))
                if result:
                    st.success("프로젝트가 생성됐습니다. 왼쪽에서 선택해주세요.")
    st.stop()

p = PROJECTS / chosen
config = read(p / "project.yaml")
audio_path = p / config["audio"]["path"]
if config["audio"].get("synthetic_test_audio"):
    st.info("파이프라인 검증용 합성 음원입니다. 콘티 그림은 배치 확인용 도식입니다.")
st.sidebar.audio(str(audio_path))
tabs = st.tabs(["01 · PROJECT", "02 · DIRECTOR", "03 · STORYBOARD", "04 · RENDER", "05 · FINAL"])

with tabs[0]:
    st.subheader("음원 분석")
    st.write((p / "input/brief.md").read_text())
    if st.button("ANALYZE", type="primary"):
        with st.spinner("음원의 비트와 에너지 변화를 분석하고 있습니다…"):
            guarded(lambda: analyze(p), "분석 완료")
    if (p / "analysis/audio.json").exists():
        data = read(p / "analysis/audio.json")
        cols = st.columns(3)
        cols[0].metric("길이", f"{data['duration_ms']/1000:.3f}s")
        cols[1].metric("추정 BPM", f"{data['tempo_bpm']:.1f}" if data['tempo_bpm'] else "미검출")
        cols[2].metric("검출 비트", len(data["beat_times_ms"]))
        st.image(str(p / "analysis/waveform.png"))
        st.caption("섹션 경계는 음향 변화 후보입니다. 벌스·후렴 명칭은 감독 검토 후 지정합니다.")

with tabs[1]:
    st.subheader("제작 패키지")
    st.write("실제 비트에 맞춘 샷과 편집 가능한 제작 문서를 준비합니다. 초기 콘티는 배치 도식이며, 실제 연출과 이미지는 Work에서 완성해 넣습니다.")
    if st.button("GENERATE PRODUCTION PACKAGE", disabled=not (p / "analysis/audio.json").exists()):
        guarded(lambda: make_package(p), "제작 패키지 생성 완료")
    bible_files = ["story.md", "style_bible.yaml", "characters.yaml", "locations.yaml"]
    for name in bible_files:
        path = p / "bible" / name
        if path.exists():
            with st.expander(name):
                content = st.text_area("내용", path.read_text(), height=260, key=name)
                if st.button("저장", key="save_"+name):
                    def save_bible():
                        if path.suffix == ".yaml":
                            value = yaml.safe_load(content)
                            if not isinstance(value, dict):
                                raise FilmError("YAML must contain an object")
                            write(path, value)
                        else:
                            atomic_text(path, content)
                    guarded(save_bible, "저장했습니다. 변경된 제작 패키지는 다시 LOCK해주세요.")
    if (p / "bible/director_request.md").exists():
        st.download_button("Work 감독 작업 지시서", (p / "bible/director_request.md").read_text(), "director_request.md")

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
                    st.image(str(p / shot["references"][0]))
                    st.caption(f"{shot['id']} · {shot['in_ms']/1000:.3f}–{shot['out_ms']/1000:.3f}s · {shot['render_mode']}")
                    with st.expander("EDIT / REPLACE", expanded=False):
                        desc = st.text_area("장면", shot["description"], key="desc_"+shot["id"])
                        instruction = st.text_area("움직임", shot["motion"]["instruction"], key="motion_"+shot["id"])
                        kind = st.selectbox("렌더 방식", ["STATIC", "LIMITED_MOTION", "FULL_GENERATIVE"], index=["STATIC", "LIMITED_MOTION", "FULL_GENERATIVE"].index(shot["render_mode"]), key="mode_"+shot["id"])
                        if st.button("연출 저장", key="edit_"+shot["id"]):
                            current = read(p / "manifest/shots.json")
                            target = next(s for s in current if s["id"] == shot["id"])
                            target.update(description=desc, render_mode=kind)
                            target["motion"]["instruction"] = instruction
                            write(p / "manifest/shots.json", current)
                            generate_storyboard(p)
                            st.rerun()
                        uploaded = st.file_uploader("승인할 첫 프레임", type=["png", "jpg", "jpeg"], key="frame_"+shot["id"])
                        if uploaded and st.button("이미지 교체", key="replace_"+shot["id"]):
                            guarded(lambda: import_frame(p, shot["id"], uploaded), "교체 완료; 다시 LOCK해주세요.")
                            st.rerun()
                        if shot.get("storyboard_kind") == "placeholder" and st.button("도식 다시 만들기", key="regen_"+shot["id"]):
                            generate_storyboard(p, shot["id"])
                            st.rerun()
        st.divider()
        reviewer = st.text_input("검토자", "Director")
        mock_only = st.checkbox("콘티 테스트용 LOCK", value=any(s.get("storyboard_kind") == "placeholder" for s in shots))
        if st.button("LOCK ALL", type="primary"):
            result = guarded(lambda: lock_production(p, reviewer, mock_only), "전체 제작 패키지를 LOCK했습니다.")
            st.rerun()

with tabs[3]:
    st.subheader("렌더링")
    renderer = st.selectbox("Renderer", ["mock", "manual", "openart"], format_func=lambda x: {"mock":"Mock · 비용 없는 콘티 테스트", "manual":"Manual · 생성한 영상 가져오기", "openart":"OpenArt · Work 작업 연결"}[x])
    quality = st.radio("Quality", ["draft", "final"], index=1, horizontal=True)
    length = st.radio("길이", ["30초 테스트", "60초 Pilot", "곡 전체"], horizontal=True)
    seconds = {"30초 테스트": 30, "60초 Pilot": 60, "곡 전체": None}[length]
    budget_cap = st.number_input("전체 크레딧 한도", min_value=0, value=int(config["budget"]["max_credits"]))
    retry_cap = st.number_input("샷당 최대 재시도", min_value=0, max_value=10, value=int(config["budget"]["max_retry_per_shot"]))
    if st.button("예산 설정 저장"):
        config["budget"].update(max_credits=budget_cap, max_retry_per_shot=retry_cap)
        write(p / "project.yaml", config)
        st.success("저장 완료")
    if st.button("예상 비용 확인"):
        prepared = guarded(lambda: prepare(p, seconds, renderer, quality))
        if prepared:
            st.session_state["estimate_"+chosen] = prepared[4]
    estimate = st.session_state.get("estimate_"+chosen)
    if estimate:
        cols = st.columns(3)
        cols[0].metric("첫 생성", f"{estimate['initial_credits']:g} credits")
        cols[1].metric("재시도 여유분", f"{estimate['retry_reserve']:g} credits")
        cols[2].metric("최대 예상", f"{estimate['worst_case_credits']:g} credits")
        if estimate["worst_case_credits"] > 0 and st.button("이 배치 비용 승인"):
            guarded(lambda: approve(p, estimate), "승인 완료")
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

with tabs[4]:
    st.subheader("완성 파일")
    outputs = sorted(f for f in (p / "output").glob("*.mp4") if ".pending." not in f.name)
    if outputs:
        selected = st.selectbox("영상", outputs, format_func=lambda f: f.name)
        st.video(str(selected))
        st.download_button("EXPORT MP4", selected.read_bytes(), selected.name, "video/mp4")
        if selected.with_suffix(".json").exists():
            st.json(read(selected.with_suffix(".json")))
    else:
        st.write("콘티를 LOCK하고 렌더링하면 여기에 영상이 나타납니다.")
