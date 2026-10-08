# ANIM-015 인코딩 경로 결정 (2026-10-08)

JunTae(프로젝트 소유자)가 2026-10-08 18:42 KST에 anim-015의 `NO_FFMPEG_ENCODING` 요구를 면제했다. 인코딩 경로는 이미 있는 FFMPEG 드라이버(`engine/encoder_backends/ffmpeg.py`)다. 제품 코드, CI 워크플로, 인코더 드라이버는 이 기록으로 바꾸지 않는다.

정규 기록: [encoding-decision.json](evidence/anim-015/encoding-decision.json).

## 결정

| 항목 | 내용 |
|---|---|
| 결정자 | JunTae (project owner) |
| 시각 | 2026-10-08 18:42 KST (`2026-10-08T18:42:00+09:00`) |
| 지시 원문 | FFmpeg 넣어 |
| 요구 | `NO_FFMPEG_ENCODING` |
| 요구 상태 | WAIVED. 시연 상태는 NOT_DEMONSTRATED이며, 부분 충족이나 자격 완료로 올리지 않는다 |
| 인코딩 경로 | FFMPEG |
| 대체한 계획 | `anim-015-gstreamer-ci` (이슈 #95). 빌드 전에 취소되었고, 구현 없이 닫혔다 |

이 결정은 설계 초안의 GStreamer CI 마감(moov 수정, CI에 GStreamer 설치, `NO_FFMPEG_ENCODING`을 기록된 범위의 부분 충족으로 보고)을 대체한다. 그 노드는 빌드되지 않았다.

## 이유

CI(`.github/workflows/ci.yml`)는 Python 3.11과 3.12 잡에서 이미 `ffmpeg`를 설치한다. GStreamer 패키지는 설치하지 않는다. GStreamer 드라이버(`engine/encoder_backends/gstreamer.py`)는 트리에 병합된 채로 둔다. CI가 그 바이너리를 제공하지 않으므로 호스트가 없고, 드라이버 파일은 수정하지 않는다. NVIDIA native, VideoToolbox, service 드라이버도 그대로다.

이 기록을 읽을 때 가져온 main은 `228cf715b5678dbc01bc7e40be891ef3c8f96b3c`이다. 그 커밋의 Compiler regression은 Python 3.11 / FFmpeg와 Python 3.12 / FFmpeg가 모두 성공했다: https://github.com/SUNBURN-Golden/film-unit-mv-studio/actions/runs/37740622736 . 이 문서 변경 자체에 대한 CI는 별도 커밋의 결과로 확인한다.

## 항목별 상태

| 항목 | 상태 | 범위와 근거 |
|---|---|---|
| FFMPEG | 인코딩 경로 | 기존 `tests/test_anim_015.py`의 FFmpeg encode/mux/verify. CI fixture만 해당한다. 프로브 fixture는 64×48, 8프레임, 24/1, H.264, yuv420p, MP4이고, end-to-end 테스트는 64×48, 10프레임이다. Python 3.11/3.12 CI 잡에서 돈다. 1920×1080이나 5760프레임 전달은 주장하지 않는다 |
| GSTREAMER | UNQUALIFIED | 드라이버는 병합되어 있으나 호스트 미검증. CI가 GStreamer를 설치하지 않는다. 드라이버는 변경하지 않는다 |
| NVIDIA_NATIVE | UNQUALIFIED | 하드웨어 미검증. 드라이버는 변경하지 않는다 |
| VIDEOTOOLBOX_NATIVE | UNQUALIFIED | 하드웨어·호스트 미검증. 드라이버는 변경하지 않는다 |
| SERVICE | UNQUALIFIED | 서비스 호스트 미검증. 드라이버는 변경하지 않는다 |
| NO_FFMPEG_ENCODING | WAIVED | 소유자 면제. 비-FFmpeg 인코드 성공은 NOT_DEMONSTRATED |
| NO_FFMPEG_RUNTIME | NOT_DEMONSTRATED | mux/verify를 포함한 비-FFmpeg 런타임 프로브는 없다. 위 면제가 이 항목을 보여 주지는 않는다 |

## 라이선스

FFmpeg는 외부 프로세스로 호출한다(`subprocess`). 이 저장소가 FFmpeg를 링크하거나 벤더링하지 않는다. FFmpeg 코어는 LGPL-2.1 이상이다. 현재 FFMPEG 드라이버의 `codec_contract`는 `libx264`를 쓰고, libx264는 GPL 구성요소다. 그런 FFmpeg를 묶어 배포하는 빌드에는 GPL 의무가 따른다. 이 결정은 새 코덱이나 GPL 전용 의존을 추가하지 않는다. GPL 구성요소가 없는 FFmpeg와 libx264가 아닌 인코더로 맞춘 LGPL-only 배포는 범위 밖이며 여기서 주장하지 않는다. 배포·번들 의무는 전달 묶음 문서(`docs/DELIVERY_PACKAGE.md`)에 속한다.
