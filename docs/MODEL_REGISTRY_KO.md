# 고정 ONNX 가중치 레지스트리

로컬 추론 가중치를 고정해 두는 레지스트리다. Git과 CI에는 가중치 파일을 넣지 않고, CI는 가중치를 받지 않는다. 이 노드는 추론을 실행하지 않는다. RIFE adapter는 여기 없다.

## 경로

`model_path(id)` 는 `$FILMUNIT_MODEL_DIR/<id>/<sha256>.onnx` 다. 환경 변수가 없으면 `~/.cache/filmunit/models/<id>/<sha256>.onnx` 다.

## 명령

```bash
filmunit model fetch <id> [--dest DIR]
filmunit model verify <id>
filmunit model status [id]
```

`fetch` 만 네트워크를 쓴다. https 만 허용하고, 리다이렉트 목적지도 https 여야 한다. 받은 파일은 새 임시 디렉터리에 두고 sha256 과 바이트 수가 핀과 같을 때만 캐시로 옮긴다. 다르면 지우고 캐시에 남기지 않는다. `verify` 는 `PRESENT` / `MISSING` / `MISMATCH` 만 알리며 어떤 파일도 지우지 않는다.

엔진을 import 할 때 소켓을 열지 않는다. `onnxruntime` 은 선택 extra `models` (`onnxruntime>=1.17,<2`, alias `rife`) 다. 핵심 의존성은 그대로다. CI는 `.[test]` 만 설치한다.

## rife49

Practical-RIFE v4.9 ONNX (MIT). 입력은 `img0`, `img1` (float32 `[1, 3, H, W]`), `timestep` (float32 `[1]`). 출력은 `output` (float32 `[1, 3, H, W]`).

| | |
|---|---|
| source | `https://huggingface.co/edgetools/rife/resolve/main/rife49.onnx` |
| mirror | `https://huggingface.co/yuvraj108c/rife-onnx/resolve/main/rife49_ensemble_True_scale_1_sim.onnx` |
| sha256 | `76e4cef9ab42fa7dd4e8f6e4aba47462051e3faa969e4bca6479784fbab0ac6f` |
| bytes | 21458882 |
| license | MIT, `https://huggingface.co/edgetools/rife/blob/main/LICENSE` |

2026-10-08에 빌더가 source URL을 받아 위 sha256·바이트 수를 계산했다. Hugging Face tree API의 LFS oid와 같고, mirror 파일의 LFS oid도 같다. 두 URL의 CDN etag는 다르지만 내용 해시는 같다.

## 자격

`qualification_state` UNQUALIFIED, `acceptance_state` PENDING, `release_state` NOT_AUTHORIZED. 가중치가 있고 onnxruntime이 import 되어도 이 모듈의 `capability_evidence` 는 `QUALIFIED_FOR_SCOPE` 가 되지 않는다. 런타임이 없으면 `UNAVAILABLE` / `missing-runtime`, 파일이 없거나 해시가 다르면 `UNAVAILABLE` / `missing-model` 이다. `capability_evidence` 의 `driver` 칸은 인코더 이름만 받으므로 레코드는 `QUALIFIED_SERVICE` 로 스키마를 통과하고, 실제 관찰은 `environment.driver = model_registry` 에 적는다. 인코드 자격이 아니다.
