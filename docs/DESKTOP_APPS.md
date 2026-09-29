# FILM UNIT · 데스크톱 앱 (Windows · macOS · Linux)

Python이나 개발 도구를 설치하지 않고 실행하는 배포입니다. 작은 실행기 창이 열리고, 기존 편집 화면이 **기본 브라우저**에 표시됩니다. 창의 **편집 화면 열기**, **프로젝트 폴더 열기**, **로그 폴더 열기**, **프로그램 종료** 버튼을 사용하세요. 프로그램 종료는 진행 중인 분석·렌더도 중단합니다. 브라우저 탭만 닫으면 서버는 계속 실행됩니다.

| 운영체제 | 받는 파일 | 실행 방법 |
| --- | --- | --- |
| Windows 10/11 x64 | `FILM_UNIT-Windows-x64.zip` | 전체를 풀고 `FILM_UNIT.exe` 더블클릭. `_internal` 폴더는 함께 두세요. |
| macOS 11 이상, Apple 실리콘(M1~) | `FILM_UNIT-macOS-arm64.zip` | 풀고 `FILM UNIT.app`을 응용 프로그램 폴더로 옮긴 뒤 실행. |
| Linux Ubuntu 22.04+/Debian 12+ 계열 | `film-unit_<버전>_amd64.deb` | `sudo apt install ./film-unit_*.deb` 후 메뉴의 **FILM UNIT** 또는 터미널 `film-unit`. |
| Linux 기타 배포판 | `FILM_UNIT-Linux-x64.tar.gz` | 풀고 `FILM_UNIT/FILM_UNIT` 실행. |

Intel Mac용은 만들지 않습니다. Linux arm64는 같은 스크립트로 빌드할 수 있지만 아직 배포하지 않습니다.

## 처음 열 때 나오는 경고 (서명되지 않은 앱)

이 앱은 코드 서명 인증서 없이 만든 **테스트 빌드**입니다. 운영체제가 게시자를 확인하지 못해 경고합니다. 이것은 바이러스 판정이 아니라 "서명 없음" 표시입니다. 각 파일의 SHA-256은 함께 배포되는 `SHA256SUMS.txt`와 비교해 확인할 수 있습니다.

- **Windows**: SmartScreen "PC를 보호했습니다" → **추가 정보 → 실행**.
- **macOS**: "확인되지 않은 개발자" 또는 "손상되었습니다" → 앱을 **우클릭 → 열기 → 열기**. 그래도 막히면 **시스템 설정 → 개인정보 보호 및 보안**에서 **그래도 열기**. 터미널을 쓴다면 `xattr -dr com.apple.quarantine "FILM UNIT.app"`.
- **Linux**: 경고 없음. `.deb`는 서명되지 않은 로컬 파일이므로 출처를 확인한 뒤 설치하세요.

## FFmpeg (영상 엔진)

FFmpeg는 이 배포에 **넣어 두지 않습니다**. 컴퓨터에 이미 있으면(`PATH`) 그것을 씁니다. 없으면 처음 한 번 내려받을지 묻습니다.

- 인터넷 연결이 필요하고 결제·구독은 없습니다.
- 공식 배포처의 **고정 버전 9.0.2**만 받고, 파일별 SHA-256이 맞지 않으면 아무것도 설치하지 않습니다.
- 출처: Windows는 Gyan.dev, macOS·Linux는 martin-riedl.de.
- Linux `.deb`는 배포판의 `ffmpeg` 패키지에 의존하므로 내려받기가 필요 없습니다.
- 필요한 기능: `libx264`, AAC, `ass`/libass 자막 필터.

## 저장 위치

| | Windows | macOS | Linux |
| --- | --- | --- | --- |
| 데이터 루트 | `%LOCALAPPDATA%\FILM_UNIT` | `~/Library/Application Support/FILM_UNIT` | `~/.local/share/FILM_UNIT` |

- `projects/` 프로젝트, `logs/` 로그, `runtime/` 내려받은 FFmpeg, `cache/` 분석 캐시, `secrets.json`·`settings.json` 모델 선택과 API 키.
- 데이터는 앱과 별도이므로 앱을 새 버전으로 교체해도 남습니다. 백업하려면 프로젝트 폴더 전체를 복사하세요.
- `FILM_UNIT_HOME`으로 데이터 루트를, `FILM_UNIT_PROJECTS`로 프로젝트 폴더를 바꿀 수 있습니다. 기존 프로젝트는 자동으로 옮기거나 덮어쓰지 않습니다.
- **API 키는 모델 선택 탭에서 이 컴퓨터에만 저장됩니다.** 프로젝트·빌드·저장소에는 들어가지 않고, 해당 서비스 주소로만 전송됩니다. Linux/macOS에서는 `secrets.json` 권한을 소유자 전용(0600)으로 저장하며, Windows는 사용자 프로필 폴더의 권한을 따릅니다.

## 만드는 범위

`음원 → 분석 → 제작 패키지 → 콘티(AI 감독 초안) → 이미지·영상 모델 선택 → Preview → 샷 교체 → LOCK → Final`

앱을 실행하거나 Preview를 컴파일하는 것만으로 유료 생성을 호출하지 않습니다. 유료 이미지·영상 요청은 견적 확인과 승인 버튼 뒤에만 실행됩니다. 가사 타이밍·LOCK·검수·Final 조건은 앱 버전과 무관하게 그대로입니다.

## 개발자 빌드

PyInstaller는 다른 운영체제용 앱을 교차 컴파일하지 못합니다. 만들려는 운영체제에서 Python 3.12로 실행합니다. Linux는 `tkinter`(`python3-tk`)가 필요합니다.

```bash
python -m pip install -e '.[test,desktop]'
python scripts/prepare_desktop_assets.py        # 고정된 한글 글꼴 내려받기
python scripts/make_icon.py                     # 아이콘 그리기
python -m PyInstaller --noconfirm desktop/film_unit.spec
python scripts/package_desktop.py               # dist/release/ 에 배포 파일 생성
python scripts/frozen_check.py dist/release/<만든 파일>   # 풀어서 실제로 실행해 검사 (Linux는 xvfb-run 사용)
```

`frozen_check.py`는 배포 파일을 사용자가 하는 것처럼 풀고, 그 안의 앱으로 6초 합성 음원 분석 → 제작 패키지 → 한글 자막 Preview → 빌드 무결성 검사 → Streamlit 화면 실행, 그리고 실행기 창의 시작/종료를 확인합니다.

GitHub Actions `Desktop apps` 워크플로가 세 운영체제에서 같은 순서로 실행하고 배포 파일과 검사 기록을 artifact로 올립니다. 성공 여부는 해당 커밋의 실제 Actions 결과로만 확인합니다. 배포 파일 안의 `BUILD_INFO.json`에는 커밋 SHA와 의존성이, `SHA256SUMS.json`에는 파일별 해시가 기록됩니다.

정식 release 게시, 코드 서명, 자동 업데이트, 스토어 배포는 포함하지 않습니다.

## 알려진 한계

- 서명되지 않았습니다(위 경고 참고).
- Linux 바이너리는 빌드한 배포판의 glibc 이상이 필요합니다(Ubuntu 22.04에서 빌드 → glibc 2.35 이상).
- 실행기 창을 열 수 없는 환경(화면 없음)에서는 창 없이 서버만 시작하고 브라우저를 엽니다. 직접 그렇게 시작하려면 `--no-gui`를 붙입니다.
- macOS·Linux 앱은 이 저장소의 GitHub Actions에서만 빌드·실행을 검사했고, 여러 컴퓨터에서의 설치 시험은 하지 않았습니다.

## 출처

Python 패키지 라이선스는 `THIRD_PARTY_LICENSES/`, Noto Sans KR은 SIL OFL로 포함합니다. FFmpeg는 사용자가 원 배포처에서 직접 받으며, 내려받은 폴더의 `download.json`에 주소와 SHA-256이 기록됩니다.

- [PyInstaller 배포 방식](https://pyinstaller.org/en/stable/operating-mode.html)
- [Streamlit 설정](https://docs.streamlit.io/develop/api-reference/configuration/config.toml)
- [Gyan FFmpeg 배포](https://www.gyan.dev/ffmpeg/builds/)
- [Martin Riedl FFmpeg 배포](https://ffmpeg.martin-riedl.de/)
- [Noto Sans KR](https://github.com/google/fonts/tree/b38c5c93af322c45f633e17ac440ec1e6c94d489/ofl/notosanskr)
