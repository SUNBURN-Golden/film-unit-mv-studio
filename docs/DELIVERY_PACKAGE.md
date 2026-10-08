# 전달 묶음

봉인된 영상 하나와, 그 파일이 어떤 빌드·profile·검수 범위인지를 확인하는 자료입니다. `LEGACY_MV`에는 이 명령이 없고, 기존 Preview/Final 컴파일은 그대로입니다.

작품 수용은 PENDING, 서비스 qualification은 UNQUALIFIED, 외부 공개는 NOT_AUTHORIZED입니다. 미리보기·fake·소스 전달은 작품 Final이 아닙니다. 파일 이름에 Final이 들어가도 승인·감독 채택·공개는 바뀌지 않습니다.

## 설치부터 묶음까지

1. Python 3.11 이상과 FFmpeg를 설치하고, 저장소에서 `python -m pip install -e '.[test]'`를 실행합니다. 데스크톱 앱으로 설치하는 경우에는 [데스크톱 앱 안내](DESKTOP_APPS.md)를 따릅니다.
2. `python -m streamlit run app/control_panel.py`로 화면을 엽니다. 기본 주소는 `http://127.0.0.1:8501`입니다.
3. 프로젝트를 만들고 음원을 분석한 뒤, 새 모드가 필요하면 `python -m engine.cli animation-init <프로젝트>`로 **명시적으로** 전환합니다. 여는 것만으로는 바뀌지 않습니다.
4. 컷을 준비하고 `compile-preview`로 전체를 봅니다. Preview는 검수된 Final이 아닙니다.
5. 컷·전환 검수와 범위 잠금이 현재 버전을 가리키면 `compile-final`로 Final 후보를 봉인합니다. 후보 봉인과 최종 승인 기록은 별개입니다.
6. 상태를 봅니다.

```bash
python -m engine.cli delivery-status <프로젝트> B0001
```

7. 묶음을 새 폴더에 씁니다. 봉인된 `builds/` 안이나 이미 있는 폴더에는 쓰지 않습니다.

```bash
python -m engine.cli delivery-bundle <프로젝트> B0001 --output /path/to/new-bundle
python -m engine.cli delivery-verify /path/to/new-bundle
```

화면에서는 `08 · ANIMATION`의 출력 단계에 같은 네 가지 상태가 글자로 표시됩니다. `전달 묶음 만들기`와 `묶음 무결성 확인`은 버튼입니다.

## 묶음 안에 있는 것

- `media/MASTER_CLEAN.mp4`, `media/MASTER_SUBBED.mp4` (있을 때)
- `media/thumbnail.png` — 전달 시퀀스 첫 프레임을 줄인 그림. 프레임이 없으면 빈 자리 표시이며 승인 범위가 아닙니다.
- `subtitles/lyrics.ass`, `subtitles/lyrics.srt` — 봉인된 자막 바이트 그대로
- `qc/diagnostics.json` — 기술 진단이 있을 때만. 승인이 아닙니다. 절대 경로와 메일 주소는 넣지 않습니다.
- `review/scope.json` — 이 빌드의 현재 검수 범위와 해시 대조
- `bundle.json`, `bundle.sha256` — 위 파일의 SHA-256

원곡 바이트는 묶음에 복사하지 않습니다. manifest의 `sources`가 봉인 해시와 현재 프로젝트 해시를 비교해 PRESERVED / DIVERGED / MISSING을 적습니다. 가사 원문·cue·LOCK 파일은 이 명령이 수정하지 않습니다. 다른 빌드의 검수나 LOCK은 이 묶음의 승인으로 이어지지 않습니다.

받은 폴더는 `delivery-verify`로 다시 해시합니다. 바이트가 다르거나, 목록에 없는 파일이 있거나, 토큰·메일·홈 경로 패턴이 있으면 실패입니다.

240초·24fps·5,760프레임은 작품 계약으로 manifest에 적힙니다. 그 프레임 수가 실제로 봉인돼 있지 않으면 `meets_work_contract`는 false입니다.

## 검증하지 못한 것

이 환경에는 실제 작품 승인, 외부 공개 권한, 유료 생성, 계정 동의가 없습니다. 브라우저에서 Tab 순서와 진행 중 스피너를 직접 확인하지 못했으면 그 부분은 미검증입니다. Streamlit AppTest는 버튼과 문구만 확인합니다.
