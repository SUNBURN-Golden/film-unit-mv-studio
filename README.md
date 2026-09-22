# FILM UNIT — ASTRA MV STUDIO v0.3

**Suno 음원과 가사를 기준으로 전체 MV를 설계하고, 샷을 교체할 때마다 새 빌드로 컴파일하는 로컬 제작 도구입니다.** 중심은 가사 타임라인, 기존 영상·콘티 선택, FFmpeg 편집, 빌드 보관입니다.

`Upload Song → Analyze → Production Package → Lyrics / Storyboard Review → Compile Preview → Replace Shots → LOCK → Compile Final`

**Preview는 완성된 애니메이션을 뜻하지 않습니다.** 영상이 없는 샷도 콘티나 임시 화면으로 연결해 곡 전체를 볼 수 있습니다. Final은 제작 LOCK, 샷 검수, 빠짐없이 검토한 가사 타이밍과 글꼴 검증을 요구합니다. 실제 외부 AI 영상 생성은 이 저장소의 검증 기록상 **0건**이며, 자동 시각 의미 QC와 완성된 실곡 MV는 아직 검증하지 않았습니다.

## 실행

Windows 사용자는 [포터블 EXE 사용·빌드 안내](docs/WINDOWS_APP.md)를 참고하세요. Python이 포함된 실행기로 기존 로컬 편집 화면을 열며, FFmpeg는 최초 실행 때 별도로 내려받습니다. 실제 다운로드 가능 여부는 해당 PR의 Windows Actions artifact에서 확인합니다.

소스 실행에는 Python 3.11 이상, FFmpeg/ffprobe, FFmpeg의 `libass` 자막 필터가 필요합니다. 동시 실행 잠금은 Linux/macOS에서 `fcntl`, Windows에서 `msvcrt`를 사용합니다.

```bash
git clone https://github.com/BeautifulMind-JT/film-unit-mv-studio.git
cd film-unit-mv-studio
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m streamlit run app/control_panel.py
```

브라우저에서 `http://localhost:8501`을 엽니다. 기본 바인딩은 127.0.0.1입니다. GitHub는 코드 보관에 사용하며, 이 로컬 제작 흐름에는 Vercel·Supabase·웹서비스 계정이 필요하지 않습니다.

## 비용 없이 전체 Preview 만들기

```bash
python -m engine.cli demo --name preview_demo --seconds 240
python -m engine.cli compile-preview projects/preview_demo
python -m engine.cli builds projects/preview_demo
```

`demo`는 합성 테스트 음원과 명시적인 임시 콘티를 만듭니다. 가사가 없으므로 Preview에는 가사 미완료 경고가 남습니다. 실제 노래·완성 애니메이션·가사 동기화 테스트를 대신하지 않습니다. 이 컴파일 명령은 외부 영상 API를 호출하지 않습니다.

각 실행은 새 `builds/B0001`, `B0002` 디렉터리를 만듭니다. 기본 Preview 화질은 최대 960px 너비이며, `--quality final`을 주면 프로젝트 출력 크기를 사용합니다. 화질을 높여도 Preview가 검수된 Final로 바뀌지는 않습니다.

## 실제 곡과 가사로 작업하기

```bash
python -m engine.cli init project_001 --audio /path/master.mp3 --brief /path/brief.md --lyrics /path/lyrics.txt
python -m engine.cli analyze projects/project_001
python -m engine.cli package projects/project_001
python -m engine.cli lyrics-prepare projects/project_001
python -m engine.cli compile-preview projects/project_001
```

입력은 최대 10분이며, 모든 샷은 측정한 곡 전체 길이를 연속해서 덮습니다. `package`의 기본값은 `CHAR_01`, `LOC_01`, 감독 검토가 필요한 중립적인 미술 방향입니다. 「물 좀 주소」 설정은 새 패키지를 만들 때만 명시적으로 선택합니다.

```bash
python -m engine.cli package projects/project_001 --preset water_please
```

이미 패키지가 있으면 덮어쓰지 않습니다. 기존 프로젝트를 수정하거나 다른 프로젝트 ID를 사용하세요. Storyboard의 기본 이미지는 제작용 캐릭터 그림이 아닌 타이밍 확인용 슬레이트입니다.

가사의 원본은 `input/lyrics.txt`입니다. `lyrics-prepare`는 원문을 보관하고 `lyrics/lyrics_timed.json`의 원문 행과 빈 타이밍을 준비합니다. **글자 수나 곡 길이로 보컬 시점을 추측하지 않습니다.** 실제 음원을 들으며 JSON을 편집하거나, 별도 정렬 도구에서 얻은 타이밍을 원문과 대조해 가져옵니다. 보컬 분리 파일을 필수로 요구하지 않으며, 자동 ASR·강제 정렬 모델은 이 컴파일러에 연결되어 있지 않습니다.

```bash
python -m engine.cli lyrics-import projects/project_001 --file /path/reviewed_lyrics_timed.json --reviewer Director
```

`--reviewer`는 실제 보컬·구절 시작과 끝·누락·반복을 검토했다는 기록입니다. 검토 전에는 생략합니다. `[hook]` 같은 반복 표시는 원문 구간을 명시적으로 연결해야 하며, 번역·새 가사·자동 생략으로 처리하지 않습니다. 원문을 바꾸면 기존 타이밍을 이력에 보관하고 다시 검토합니다. 영상 컷 편집은 가사 타이밍을 움직이지 않습니다.

검수와 전체 LOCK은 별도로 관리합니다.

| 수정 | 다시 검토할 대상 |
|---|---|
| 가사 타이밍·자막 설정·폰트 | 가사 검수와 전체 LOCK; 영상 검수 유지 |
| 두 샷 사이 컷 | 인접 두 샷과 전체 LOCK; 다른 샷 검수 유지 |
| 한 샷의 콘티·영상·사용 구간 | 해당 샷; 콘티·연출 변경 시 전체 LOCK |
| 공통 스타일·캐릭터·이야기·가사 원문 | 전체 영상 검수와 LOCK; 원문 변경은 가사도 재검토 |

검수 기록은 새 범위를 증명하는 schema 2로 저장합니다. 이전 전역 해시 기반 영상 검수나 타이밍만 포함한 가사 검수는 자동 승계하지 않으므로 한 번 다시 검토해야 합니다. 과거 빌드의 파일은 그대로 보존됩니다.

## 한글 자막과 Final

한글·영문 등 실제 가사의 모든 문자를 포함하는 글꼴을 프로젝트 안에 두고 `project.yaml`에 지정하세요. 짧은 샘플용 글꼴 부분 집합은 전체 곡의 문자를 보장하지 않습니다.

```yaml
subtitles:
  font_name: Noto Sans KR
  font_file: input/fonts/NotoSansKR-Regular.ttf
  font_size: 48
  margin_x: 72
  margin_bottom: 76
```

글꼴 파일은 별도로 준비해야 합니다. 지정하지 않으면 시스템 글꼴을 찾지만, 기본 DejaVu Sans는 한글 전체를 지원하지 않습니다. Final은 실제 cue 문자에 대한 cmap 검증이 실패하거나 확인되지 않으면 중단합니다. 선택한 글꼴은 빌드에 복사됩니다. 자막은 좌우 최소 5%, 아래 최소 7%의 여백을 사용합니다. 긴 구절의 줄바꿈과 읽기 속도는 감독이 Preview에서 확인해야 합니다.

검토한 첫 프레임을 가져오고 제작 LOCK을 한 뒤, 완성 샷 영상은 검토자와 근거를 기록해 선택합니다.

```bash
python -m engine.cli import-frame projects/project_001 S001 /path/S001.png
python -m engine.cli lock projects/project_001 --reviewer Director
python -m engine.cli import-asset projects/project_001 S001 /path/S001.mp4 --kind final --reviewer Director --evidence "인물, 화풍, 움직임, 소품과 불필요한 글자를 확인함"
python -m engine.cli compile-final projects/project_001
```

위 예의 한 샷뿐 아니라 모든 필요한 샷과 가사가 조건을 충족해야 Final이 나옵니다. 의도적으로 `STATIC`으로 승인한 샷은 LOCK된 가져온 이미지를 사용할 수 있습니다. `LIMITED_MOTION`과 `FULL_GENERATIVE`에는 검토한 최종 영상이 필요합니다. 콘티 테스트용 LOCK은 Final을 승인하지 않습니다.

## 샷 교체와 빌드 이력

| 작업 | 명령 예시 |
|---|---|
| 초안 영상 선택 | `import-asset projects/project_001 S024 /path/take.mp4 --kind draft` |
| 샷 분할 | `split-shot projects/project_001 S024 --at-ms 120700` |
| 인접한 같은 시퀀스 샷 병합 | `merge-shots projects/project_001 S024 S025` |
| 다음 샷과의 컷 이동 | `move-cut projects/project_001 S024 --at-ms 125700` |
| 측정 비트·온셋에 맞추기 | `snap-cut projects/project_001 S024 --to beat` |
| 전체 재컴파일 | `compile-preview projects/project_001` |
| 저장 빌드 무결성 검사 | `verify-build projects/project_001/builds/B0001` |
| 저장 입력으로 다시 편집 | `replay-build projects/project_001/builds/B0001 --output /path/replay_B0001` |

표의 명령 앞에는 `python -m engine.cli`를 붙입니다. 분할한 새 샷은 독립된 콘티 파일을 갖습니다. 오른쪽 그림을 교체해도 왼쪽 이미지 바이트는 유지됩니다. 컷 변경은 해당 샷 선택과 검수를 다시 요구하며 기존 빌드·가사 시간은 유지합니다. 빌드에는 선택한 원본, 정규화한 샷 영상, 원곡, 제작 문서, 자막, 글꼴, SHA-256 목록이 복사됩니다. 원본 프로젝트가 바뀌어도 과거 빌드의 MP4를 열거나 저장 입력으로 재편집할 수 있습니다. 다른 FFmpeg 버전에서 새로 인코딩한 결과의 바이트까지 동일하다고 보장하지는 않습니다.

완료 빌드의 기본 결과물은 다음 네 개입니다.

- `MASTER_CLEAN.mp4`
- `MASTER_SUBBED.mp4`
- `lyrics.ass`
- `lyrics.srt`

`build.json`의 PREVIEW/FINAL 모드, 선택 자산 종류, 미완료 샷, 가사 경고를 함께 확인하세요. Preview의 `COMPLETE`는 편집 파일 생성 완료를 뜻합니다. 소스·범위·화질이 같으면 로컬 정규화 캐시를 재사용합니다. 실패한 빌드도 별도 번호로 남으며 완료 빌드를 덮어쓰지 않습니다.

## 기존 프로젝트와 영상 생성

```bash
python -m engine.cli migrate projects/existing_project
```

명시적 migration은 원래 `project.yaml`을 백업하고 데이터 버전을 갱신합니다. 기존 음원·샷·콘티·LOCK·유료 take 파일은 다시 쓰지 않습니다. 프로젝트 3 / 샷 2 / 가사 1 / 빌드 1 / 오디오 1 버전은 패키지 0.3.0과 별도로 관리합니다.

기존 `compile` 명령과 UI의 **샷 생성 · 고급**은 Mock/Manual/OpenArt/fal/Economy 렌더·QC·이어하기 경로입니다. `compile-preview`와 다르게 선택한 렌더러에 따라 유료 요청을 준비하거나 제출할 수 있어 기존 견적·배치 승인·예산·중복 제출 방지를 유지합니다. **새 컴파일 명령 자체는 영상 생성을 요청하지 않습니다.** API 연결 없이도 완성한 외부 샷을 `import-asset`으로 사용할 수 있습니다.

Economy/fal/benchmark는 호환성을 위해 기존 파일 위치에 남겨 둔 실험 기능입니다. 이번 버전에서는 새 provider를 추가하지 않았습니다. OpenArt는 기존 Work 작업 파일을 이용한 image-to-video 연결이며 다중 element/reference 영상, 자동 시각 의미 QC, 숫자 QC의 PASS/FAIL 전환은 후속 작업입니다. [기존 OpenArt 운영](docs/OPENART_BRIDGE.md), [Economy 운영](docs/ECONOMY_COMPILER.md).

## 검증 범위

```bash
python -m pytest -q
python -m pytest tests/test_compiler_v03.py -q
```

새 핵심 회귀는 **240초 / 48샷 / final 7·draft 13·storyboard 25·placeholder 3 / 가사 82줄**을 실제 MP4로 편집하고 한 샷을 바꾼 다음 나머지 47개 소스 해시·음원 해시·가사 타이밍이 유지되는지 검사합니다. CI 비용을 줄이기 위해 합성 음원과 320×240 영상을 사용합니다. 이 테스트는 실제 가창 정렬, 생성 모델의 애니메이션 품질, 1080p 실곡 완성을 입증하지 않습니다.

GitHub Actions에는 Ubuntu / Python 3.11·3.12 / FFmpeg·CJK 글꼴 / pytest가 구성되어 있습니다. 원격 실행 성공 여부는 해당 커밋의 Actions 결과로 확인해야 합니다. [v0.3 실행 기록](docs/V03_ACCEPTANCE_REPORT.md)과 이전 30초·60초 [Mock 검증 기록](docs/ACCEPTANCE_REPORT.md)을 함께 제공합니다.

원본 음원 파일은 SHA-256으로 보호하고 분석용 WAV를 따로 만듭니다. 최종 MP4는 그 원곡만 AAC 320kbps로 인코딩하며 생성 클립의 소리는 제거합니다. AAC는 손실 형식이므로 원본 파일과 비트 단위로 같지는 않습니다. 정수 ms 컷을 곡 전체 기준 프레임으로 반올림해 샷별 누적 드리프트를 막습니다.
