# 저가 2D 애니메이션 비교 테스트

확인일: 2026-09-11. 실제 유료 생성 **0건**, 영상 생성비 지출 **0**. 연결 코드와 비교 입력을 준비했으며, 두 모델의 화질 비교는 아직 실행하지 않았다.

## 비교 조건

동일한 첫 프레임에서 시작하는 독립적인 5초 장면 3개를 각각 생성한다. 고정 카메라 안에서 인물의 몸과 얼굴이 움직여야 한다.

| 샷 | 동작 | 확인할 실패 |
|---|---|---|
| S001 | 컵에서 손을 떼고 왼쪽으로 두 걸음 | 발 미끄러짐, 다리 변형, 카메라만 움직임 |
| S002 | 컵을 약 15cm 들었다 내려놓기 | 손가락·손잡이 합쳐짐, 컵 변형 |
| S003 | 눈 깜박임, 컵 쪽 시선 이동, 옅은 미소 | 얼굴 교체, 과도한 표정, 줌으로 동작 대체 |

새로 만든 시험용 그림이며 본편 CHAR_A·CHAR_B 확정안이 아니다. 15초 무음 WAV는 노래가 아닌 타이밍 테스트 입력이다. QC의 5개 샘플 프레임과 실제 재생을 함께 검토한다. 프레임만으로 자연스러운 동작을 판정하지 않는다.

## 생성비

| 후보 | 설정 | 최초 3개 | 샷마다 최대 2회 추가 생성 포함 |
|---|---|---:|---:|
| fal Wan 2.2 Turbo | 720p, 약 5초/개 | USD 0.30 | USD 0.90 |
| OpenArt PixVerse V6 | 720p, 5초, 음성 끔 | 210 credits | 630 credits |

fal [공식 모델 페이지](https://fal.ai/models/fal-ai/wan/v2.2-a14b/image-to-video/turbo)는 720p 한 영상당 USD 0.10을 표시했다. 공식 샘플 파일은 5.03125초였다. 모든 결과의 길이 보장으로 취급하지 않으며, 컴파일러는 실제 결과가 샷보다 짧으면 거부한다. PixVerse 금액은 인증된 OpenArt 견적 조회 결과다. 실제 제출 전 reference와 설정을 포함해 재조회한다. 크레딧과 달러를 합산하거나 임의 환율로 바꾸지 않는다.

전체 240초를 5초씩 48개 생성한다면 fal 최초 USD 4.80, 모든 샷에 2회씩 재시도하면 USD 14.40이다. PixVerse는 각각 3,360 / 10,080 credits다. 실패율과 최종 화질은 미측정이다. 이미지 제작, 버리는 앞뒤 프레임, 업스케일, 세금과 최소 충전 금액은 제외했다. 본편을 정지 화면으로 채워 비용을 줄이는 계획이 아니다.

## 입력 재현

```bash
python -m engine.cli benchmark --reference /absolute/path/test_frame.png
```

`projects/animation_benchmark/`에 무음 음원 분석, Bible, 세 샷 manifest, 첫 프레임, contact sheet, storyboard.html, 비교 계획을 만든다. 자동 LOCK·비용 승인·외부 제출은 하지 않는다. 다른 이름은 `--name`으로 지정한다. 다운로드한 테스트 프로젝트는 저장소 `projects/` 아래에 풀면 Control Panel에서 선택할 수 있다.

## fal 실행

1. fal 계정에서 API 사용을 위한 결제·잔액을 준비하고, 실행 환경에 `FAL_KEY`를 설정한다. 키를 채팅이나 Git에 올리지 않는다.
2. Control Panel에서 Renderer를 `fal`로 설정한다. 기존 프로젝트는 **Wan 테스트 설정 추가**로 설정 파일을 만든다. CLI에서는 `templates/fal_wan_turbo.json`을 프로젝트의 `render/fal_config.json`으로 복사한다.
3. 공식 가격을 확인한다. 견적이 만료되면 중단한다. 동일 가격을 재확인했을 때만 `price_valid_until`을 갱신한다. 가격이 바뀌면 adapter 가격표와 승인 비용도 함께 갱신한다.
4. 그림과 동작을 검토하고 일반 LOCK을 건다. `fal 전체 예산 (USD)`을 0.90으로 저장하면 세 샷의 최대 재시도까지 포함한다. 기본 한도는 0이다.
5. 예상 비용 확인 → 이 배치 비용 승인 → GENERATE / RESUME. 다음 실행은 같은 요청 ID의 상태를 조회한다. 생성 후 의미 QC를 기록하고 RESUME한다.

```bash
python -m engine.cli lock projects/animation_benchmark --reviewer Director
python -m engine.cli estimate projects/animation_benchmark --renderer fal --quality draft
# 표시된 금액과 실제 estimate_id를 확인한 뒤 승인한다.
python -m engine.cli approve projects/animation_benchmark --estimate-id ACTUAL_ESTIMATE_ID
python -m engine.cli compile projects/animation_benchmark --renderer fal --quality draft
```

Wan adapter는 5초 이하 샷과 480p/580p/720p를 지원한다. 1080p 편집 출력은 원본 생성 해상도를 높이지 않는다. 긴 샷은 먼저 분할·검토한다. `aspect_ratio=auto`로 첫 프레임 비율을 전달하며 실제 출력은 생성 후 확인한다. 문서에 없는 `generateAudio` 파라미터는 보내지 않고, 최종 편집에서 영상의 소리를 모두 제거한다. prompt 확장은 꺼서 감독의 동작 지시를 보존한다.

## 재개와 비용 제한

- 달러와 OpenArt 크레딧은 별도 ledger 합계와 한도로 관리한다. 실패·대기 작업도 예약액에 남긴다.
- POST 전에 SUBMITTING을 기록한다. 요청 중 타임아웃이나 종료가 나면 배치를 중단한다. `render/fal_jobs/<job>.json`과 fal 이력에서 기존 요청을 대조한 뒤 실제 request ID·status URL·response URL을 복구한다. 확실한 실패 근거 없이 파일 삭제나 새 ID 재제출을 하지 않는다.
- 다운로드·상태 조회 실패는 기존 요청을 재조회한다. 의미 QC 실패 또는 provider가 확인한 생성 실패만 다음 attempt를 사용한다.
- fal 서버의 자동 재시도는 끄고 컴파일러의 한도를 적용한다. [fal Queue 공식 문서](https://fal.ai/docs/documentation/model-apis/inference/queue)
- API 키는 fal queue 호스트에만 보낸다. 결과 다운로드에는 인증 헤더를 보내지 않는다.
- 가격·비용 단위, 모호한 제출, 재개, 다운로드 실패, provider 실패 수정, 기존 Mock·QC 동작을 포함한 **18개 테스트가 통과**했다. 테스트는 오류를 주입한 로컬 fixture이며 유료 API 성공의 증거는 아니다.

## 남은 단계

실행 환경에 FAL_KEY가 없고, OpenArt 계정은 Free / 40 credits다. PixVerse 720p 한 샷에 필요한 70 credits보다 적다. 연결·잔액 준비 후 그림과 동작을 LOCK하고 실제 결과를 비교해야 한다. 4분 본편 품질은 짧은 테스트를 통과한 뒤 판단한다.
