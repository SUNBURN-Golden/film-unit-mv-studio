# ANIM-015 RIFE 어댑터 결정

RIFE v4.9 CPU ONNX는 프레임 보간기다. path-B 구간 어댑터 `rife_onnx`로만 붙는다. 인코더가 아니므로 `no_ffmpeg_encoding`은 항상 false다. 인코드는 기존 FFMPEG 드라이버다. 2026-10-08에 JunTae가 `NO_FFMPEG_ENCODING`을 면제했다. 기록은 [인코딩 경로 결정](ANIM_015_ENCODING_DECISION_KO.md).

가중치는 git과 CI에 없다. CI는 `.[test]`만 설치하고, 테스트는 주입한 FAKE 세션으로 돈다. 런타임이나 핀이 없으면 `CAPABILITY_UNAVAILABLE`이며 블렌드나 fake 대체는 없다.

## 채택

| 항목 | 내용 |
|---|---|
| 모델 | Practical-RIFE v4.9 ONNX, 레지스트리 `rife49` |
| 실행 | `CPUExecutionProvider`만, 스레드 `min(4, cpu_count)` |
| 계약 | `capabilities` / `quote` / `submit` / `status` / `cancel`. provider `LOCAL_TOOL`, 자격 `UNQUALIFIED`, 비용 0 credits |
| 끝점 | 양끝 반환 (`START_END_INCLUDED`). import가 공유 end anchor를 제거한다. start-only는 거부 |
| 컷 | `[start, end)`가 샷 플랜의 구간 경계를 넘으면 거부 |
| 산출 | `animation/rife_onnx/output/<request_id>/`. 가져온 세그먼트는 DRAFT이며 사람 컷 검토 전이다 |
| 인코드 | `engine/encoder_backends/ffmpeg.py`. GStreamer 단계는 없다 |

모델 sha256과 onnxruntime 버전은 잡 recipe digest에 들어간다. 핀이나 런타임이 달라지면 잡 식별자가 달라진다.

## 거부한 대안

설계 초안의 후처리 엔진 레이어는 만들지 않았다. 구간 어댑터, 시퀀스, 인코더 드라이버가 이미 있다.

| 후보 | 결정 |
|---|---|
| Real-ESRGAN, faster-whisper | 이번 노드 아님. 각각 저해상도 생성기와 측정된 보컬 시험 뒤에만 |
| Demucs, SAM 2, MMagic, Video Depth Anything | torch·용량·유지보수 또는 제품 역할이 없음 |
| Anime4K, waifu2x-ncnn-vulkan | GPU/Vulkan. 이 박스는 대상이 아님 |
| PySceneDetect | 컷은 `timeline/edit.json`에 적힌다. 검출하지 않는다 |
| OpenTimelineIO | 교환 요구가 없다 |
| GStreamer로 이 노드의 인코드를 닫기 | `anim-015-gstreamer-ci`는 빌드 전에 취소됨 |
| 컷을 넘는 보간, twos를 일괄 ones로 바꾸기 | 설계 §7에서 금지 |

## 자격

| 항목 | 상태 | 범위 |
|---|---|---|
| 제작 인비트윈 | UNQUALIFIED | 예술적 수용은 사람 컷 검토만 |
| 처리량 | UNQUALIFIED | 주장하지 않음 |
| 1080p / 5760프레임 보간 | UNQUALIFIED | 돌리지 않음 |
| `NO_FFMPEG_ENCODING` | false | 보간기는 인코드 증거가 아님. 면제 기록은 인코딩 결정 문서 |
| `NO_FFMPEG_RUNTIME` | NOT_DEMONSTRATED | mux/verify는 FFmpeg |
| 이 노드의 데모 | UNQUALIFIED, 데모를 실제로 돌리고 증거를 남기면 그 범위만 PARTIAL | `docs/evidence/anim-015-rife/demo.json` |

`model status`의 `rife_onnx` 증거가 `QUALIFIED_FOR_SCOPE`가 되는 경우는 이 호스트에서 실제 세션이 두 프레임 fixture를 끝내고 출력 해시를 기록했을 때뿐이다. 그 범위는 그 fixture다. 주입한 FAKE 세션은 `DOCUMENTED_ONLY`다.
