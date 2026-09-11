# FILM UNIT — ASTRA MV STUDIO v0.1

음원 분석 → 제작 문서 → 샷·콘티 → LOCK → 렌더링 → QC → FFmpeg 편집 → MP4.

**MockRenderer로 30초와 60초의 실제 MP4 출력을 검증한 로컬 제작 도구입니다.** 이번 샘플은 합성 테스트 음원과 콘티 배치 도식입니다. Suno 음원이나 완성된 「물 좀 주소」 MV가 아닙니다. 실제 생성형 영상은 아직 제출하지 않았으며 사용 크레딧은 0입니다.

**최종 목표는 인물의 행동·표정과 장면 속 요소가 실제로 움직이는 2D 애니메이션 MV입니다.** 고정 카메라에서도 피사체는 움직입니다. 새 제작 패키지는 모든 샷을 LIMITED_MOTION 초안으로 만들고, 감독이 복잡한 동작이나 의도적인 정지 구간을 지정합니다. 정지 이미지에 pan/zoom만 적용한 Mock 영상은 콘티·타이밍 테스트용입니다.

## 실행

Python 3.11+와 FFmpeg/ffprobe가 필요합니다. 검증 환경은 Linux / Python 3.12입니다. macOS에서도 사용할 수 있으며 Windows는 WSL을 권장합니다. 프로젝트 동시 실행 잠금에 `fcntl`을 사용합니다.

```bash
cd FILM_UNIT
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m streamlit run app/control_panel.py
```

로컬 브라우저에서 `http://localhost:8501`을 엽니다. 사용자 계정·웹 호스팅은 필요하지 않습니다. 서버는 기본적으로 127.0.0.1에만 바인딩합니다. 공개 SaaS용 인증·작업 큐·멀티테넌시는 포함하지 않습니다.

## 지금 바로 재현하는 테스트

```bash
python -m engine.cli demo --name my_test --seconds 65
python -m engine.cli compile projects/my_test --seconds 30 --output TEST_A_30S.mp4
python -m engine.cli compile projects/my_test --seconds 60 --output PILOT_60S.mp4
python -m pytest -q
```

`demo`는 자체 합성 음원을 만들고 테스트 전용 LOCK을 설정합니다. 실제 프로젝트에서는 사람이 콘티를 검토하고 LOCK해야 합니다. `--seconds`를 생략하면 동일한 전체 곡 manifest를 사용해 곡 끝까지 편집합니다. 입력은 최대 10분입니다.

전달 ZIP에는 `projects/pipeline_demo/output/TEST_A_30S.mp4`, `PILOT_60S.mp4` 샘플이 포함됩니다. GitHub에서는 미디어를 추적하지 않으므로 위 `demo` 명령으로 재현합니다.

## 실제 Suno 음원으로 시작하기

1. Control Panel에서 새 프로젝트 → MP3/WAV와 creative brief 업로드.
2. ANALYZE → GENERATE PRODUCTION PACKAGE.
3. Work에서 `bible/director_request.md`와 입력·분석 파일을 바탕으로 이야기와 실제 그림을 제작합니다. 파일의 이야기·나이·장소·샷 설명을 검토하고, 각 샷의 승인할 첫 프레임을 업로드합니다.
4. STORYBOARD에서 전체 확인 → LOCK ALL. 도식만으로 테스트할 때는 “콘티 테스트용 LOCK”을 선택합니다.
5. Mock은 무료 로컬 렌더입니다. 실제 영상은 Manual로 가져오거나 [OpenArt 연결 안내](docs/OPENART_BRIDGE.md)에 따라 Work에서 생성합니다. 저가 후보인 fal Wan 2.2 Turbo와 OpenArt PixVerse V6는 [애니메이션 비교 테스트](docs/CHEAP_ANIMATION_TEST.md)를 참고합니다.
6. 비용을 확인하고 해당 배치를 승인 → GENERATE / RESUME → QC 검토 → EXPORT.

```bash
python -m engine.cli init project_001 --audio /path/master.wav --brief /path/brief.md
python -m engine.cli analyze projects/project_001
python -m engine.cli package projects/project_001
python -m engine.cli lock projects/project_001 --reviewer Director --mock-only
python -m engine.cli compile projects/project_001 --seconds 30
```

## 구현 범위

| 기능 | 현재 상태 |
|---|---|
| 실제 음원 길이·BPM·비트·onset·에너지·무음·피크 | 구현·실행 검증 |
| 곡 전체 shot manifest / 30·60초 창으로 편집 | 구현·실행 검증 |
| Style / Character / Location Bible | 편집 가능한 템플릿·LOCK 구현 |
| 서사·캐릭터 그림 자동 창작 | Work 감독 단계; 독립 Python LLM 호출은 미연결 |
| 콘티 PNG·contact sheet·HTML | 구현; 기본값은 명시적 배치 도식 |
| Mock / Manual / OpenArt / fal abstraction | 구현; fal은 오류 주입 검증, 실제 유료 호출 미검증 |
| OpenArt PixVerse V6 | 첫 프레임 schema·견적·AUTO 선택 지원 |
| fal Wan 2.2 Turbo | 달러 예산·비동기 요청·재개·다운로드 구현; API 키 필요 |
| OpenArt AUTO routing | 검증된 form·견적 설정이 있는 모델만 선택 |
| 독립 로컬 앱에서 OpenArt 직접 호출 | 미구현; Work MCP 작업 파일 방식 사용 |
| 기술 QC·5프레임 추출 | 자동 |
| 인물·화풍·소품·텍스트·동작 QC | 근거가 있는 검토 기록을 읽는 인터페이스; 무인 시각 모델 미연결 |
| QC 실패 수정 지시·최대 2회 재시도·이어하기 | 구현·오류 주입 검증 |
| H.264 1440×1080 24fps + 원곡 AAC | 30초·60초 출력 검증 |
| 실제 AI 영상 3샷 | 연결 준비 및 요청 파일 테스트; 실제 생성 미실행 |
| Git | 비공개 GitHub 저장소에 소스와 개발 이력 관리 |

## 결과물

`output/`에는 선택한 메인 MP4, `YOUTUBE.mp4`, `teaser_30s.mp4`, `poster.jpg`, `thumbnail.jpg`, `metadata.md`와 JSON 검증 기록이 생깁니다. Mock 기본 이름은 `ANIMATIC.mp4`, 실제 외부 영상을 검토해 편집하는 경우는 `MASTER.mp4`입니다. 샘플의 `PILOT_60S.mp4`도 Mock임이 영상·메타데이터·QC에 표시됩니다.

원본 음원 파일의 SHA-256은 전후 동일합니다. 분석용 WAV는 별도로 만들며 원본을 덮어쓰지 않습니다. 완성 MP4의 AAC 320kbps는 손실 인코딩이므로 원본과 비트 단위로 동일하지 않습니다. 음량 정규화·속도 변경·영상 생성 모델의 소리 혼합은 하지 않습니다. Pilot은 원곡 시작부터 요청 길이만 사용합니다.

24fps의 프레임 경계는 약 41.667ms입니다. 원시 timecode는 정수 ms로 보존하고, 각 경계를 **곡 전체 기준**으로 반올림해 프레임을 배분합니다. 샷마다 독립적으로 길이를 반올림하지 않아 누적 싱크 오차가 생기지 않습니다. 음악 특징 검출의 시간 해상도는 약 23.22ms이며 추정치입니다.

초기 브리프의 STATIC 40% 비용 절감 비율은 현재 제작 목표에 적용하지 않습니다. LIMITED_MOTION도 인물·장면 요소의 실제 애니메이션이 필요하며, 현재 로컬 renderer의 hold/pan/zoom만으로 이를 구현하지는 못합니다. 4분 본편은 우선 전체 240초의 애니메이션을 기준으로 예산을 잡고, 실제 콘티에서 승인한 정지 구간·재사용 및 각 생성 클립의 과금 길이를 반영해 견적을 확정합니다.

## Git 보관과 GitHub

제공 ZIP 안의 `film-unit-history.bundle`로 Git 이력을 복원할 수 있습니다.

```bash
git clone film-unit-history.bundle film-unit-source
```

소스 저장소: [BeautifulMind-JT/film-unit-mv-studio](https://github.com/BeautifulMind-JT/film-unit-mv-studio). 비공개 저장소이며 개발 단계별 커밋을 보존합니다.

```bash
git clone https://github.com/BeautifulMind-JT/film-unit-mv-studio.git
cd film-unit-mv-studio
```

음원·생성 영상·API 설정은 `.gitignore`에서 제외했습니다. 공유할 미디어는 별도 제공하고 코드 저장소는 가볍게 유지합니다.

## 참고

- [Streamlit file uploader 공식 문서](https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader)
- [librosa 공식 프로젝트](https://github.com/librosa/librosa)
- [FFmpeg 공식 문서](https://ffmpeg.org/ffmpeg.html)
- 이번 환경에서 조회한 OpenArt 모델·form 스냅샷: `templates/`. 실제 제출 전에는 다시 조회해야 합니다.
