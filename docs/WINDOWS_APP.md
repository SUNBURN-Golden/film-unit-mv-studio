# FILM UNIT · Windows 앱

Windows 10/11 x64용 포터블 배포입니다. ZIP 전체를 풀고 **FILM_UNIT.exe**를 더블클릭합니다. EXE 옆의 `_internal` 폴더도 반드시 함께 보관하세요. Python·WSL·개발 도구를 따로 설치하지 않습니다.

작은 실행기 창이 열리고, 기존 편집 화면이 기본 브라우저에 표시됩니다. 창의 **편집 화면 열기**, **프로젝트 폴더 열기**, **프로그램 종료** 버튼을 사용하세요. 프로그램 종료는 진행 중인 분석·렌더도 중단합니다. 브라우저 탭만 닫으면 서버는 계속 실행됩니다.

## 첫 실행과 저장 위치

- Python, 컴파일러, 기본 preset, 한글 Noto Sans KR 글꼴을 포함합니다.
- FFmpeg/ffprobe가 없으면 최초 한 번 **FFmpeg 다운로드** 확인창이 나옵니다. 인터넷 연결이 필요하며 유료 생성·구독 결제는 없습니다. Gyan의 고정 버전 9.0.2와 SHA-256을 검증한 다음 설치합니다. FFmpeg 바이너리는 이 ZIP에 재배포하지 않습니다.
- 이미 `PATH`에 있는 FFmpeg/ffprobe도 사용할 수 있습니다. `libx264`, AAC, `ass`/libass 지원이 필요합니다.
- 기본 프로젝트 위치: `%LOCALAPPDATA%\FILM_UNIT\projects`
- 로그: `%LOCALAPPDATA%\FILM_UNIT\logs`
- 내려받은 FFmpeg와 분석 캐시도 `%LOCALAPPDATA%\FILM_UNIT` 아래에 있습니다.
- `FILM_UNIT_HOME` 환경변수로 데이터 루트를, `FILM_UNIT_PROJECTS`로 기존 프로젝트 폴더를 지정할 수 있습니다. 기존 프로젝트는 자동 이동하거나 덮어쓰지 않습니다.

새 프로젝트에는 번들 한글 글꼴을 독립 복사합니다. 기존 프로젝트의 글꼴·LOCK·검수는 임의 변경하지 않습니다. 데이터는 앱과 별도이므로 앱 ZIP을 새 버전으로 교체해도 유지됩니다. 백업하려면 프로젝트 폴더 전체를 복사하세요.

## 만드는 범위

`음원 업로드 → 분석 → 제작 패키지 → 콘티/가사 검토 → Preview → 샷 교체 → LOCK → Final`

**EXE는 현재 컴파일러를 설치하기 쉽게 묶은 것입니다.** 영상이 없는 장면은 Preview에서 콘티/임시 화면으로 보이며, 자동 완성 애니메이션으로 바뀌지 않습니다. 가사 타이밍은 실제 보컬을 들으며 별도로 입력·검토해야 합니다. Final의 가사·영상·LOCK 조건은 그대로입니다.

외부 영상은 기존 Manual 가져오기/Work bridge를 사용합니다. OpenArt 구독·ChatGPT 연결·API 키가 EXE로 자동 이전되지 않습니다. 앱을 실행하거나 Preview를 컴파일하는 것만으로 유료 영상 API를 호출하지 않습니다.

## 개발자 빌드

실제 Windows x64에서 Python 3.12로 실행합니다. PyInstaller는 다른 OS용 EXE를 교차 컴파일하지 않습니다.

```powershell
python -m pip install -e '.[test,desktop]'
python scripts/prepare_desktop_assets.py
python -m PyInstaller --noconfirm desktop/film_unit.spec
python scripts/package_desktop.py
```

결과는 `dist/FILM_UNIT/`입니다. 이 디렉터리를 통째로 ZIP으로 묶습니다. 첫 실행은 FFmpeg 설치를 제안합니다. 설치 후에는 다음 명령으로 EXE 내부의 실제 엔진·한글 자막·UI 실행을 검사할 수 있습니다.

```powershell
$env:FILM_UNIT_HOME = "$env:TEMP\FILM_UNIT_test"
$p = Start-Process .\dist\FILM_UNIT\FILM_UNIT.exe -ArgumentList '--self-test', 'C:\temp\film-unit-smoke' -PassThru -Wait
if ($p.ExitCode -ne 0) { throw 'Frozen smoke failed; check application logs' }
```

`Windows portable app` Actions는 Windows에서 EXE를 빌드하고, 6초 합성 음원 분석→제작 패키지→실제 한글 자막 Preview→빌드 무결성 검사, Streamlit AppTest, 실행기 시작/종료를 검사합니다. 기존 Ubuntu `Compiler regression`도 별도로 유지합니다. 성공은 해당 커밋의 실제 Actions 결과로 확인해야 합니다. `BUILD_INFO.json`에 SHA/의존성, `SHA256SUMS.json`에 배포 파일 해시를 기록합니다.

배포는 서명되지 않은 테스트 빌드입니다. Windows가 게시자를 확인하지 못할 수 있습니다. 코드 서명 인증서·자동 업데이트·스토어 배포는 포함하지 않습니다. 자동 병합 또는 정식 release 승인은 별도입니다.

## 의존성과 출처

Python 패키지 라이선스는 `THIRD_PARTY_LICENSES/`, Noto Sans KR은 SIL OFL로 포함합니다. FFmpeg는 첫 실행 때 사용자가 원 배포처에서 직접 받습니다. 해당 런타임의 LICENSE/README와 `download.json`을 함께 보관합니다.

- [PyInstaller 배포 방식](https://pyinstaller.org/en/stable/operating-mode.html)
- [Streamlit 설정](https://docs.streamlit.io/develop/api-reference/configuration/config.toml)
- [Gyan FFmpeg 배포](https://www.gyan.dev/ffmpeg/builds/)
- [Noto Sans KR](https://github.com/google/fonts/tree/b38c5c93af322c45f633e17ac440ec1e6c94d489/ofl/notosanskr)

이 문서는 사용·검증 방법이며, 아직 완료하지 않은 CI나 정식 배포를 완료했다고 주장하지 않습니다.
