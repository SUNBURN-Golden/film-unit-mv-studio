# ASTRA MV COMPILER v0.2 — Work 제작·편집 운영

사용자는 이야기·콘티와 구체적인 생성 예산을 승인한다. Work가 장면별 도구 선택, 생성 결과 확인, 필요한 수정과 편집을 이어서 수행하도록 기존 로컬 컴파일러를 확장했다. 실제 캐릭터 애니메이션이 목표다. 실패한 동작을 정지 이미지 확대·이동으로 대체하지 않는다.

이번 구현에는 유료 영상 생성·구독·충전이 없다. 실제 모델의 완성도와 4분 본편 비용은 아직 검증하지 않았다. Python 엔진은 작업을 재개할 수 있지만, 외부 영상 결과 카드와 의미 QC를 처리하는 무인 백그라운드 에이전트는 연결되지 않았다.

## 비용을 줄이는 동작

| 기능 | 실제 동작 |
|---|---|
| 장면별 후보 | 검증된 OpenArt form·견적 또는 fal 설정만 사용 |
| 저가 순서 | 같은 서비스의 유효 견적끼리 가격순 선택; 서비스 간에는 지정한 우선순위 적용 |
| 실패 컷 재시도 | 기본 최초 생성 → 같은 모델에 구체적 수정 1회 → 승인된 다음 후보 1회 |
| 원본 재사용 | 동일한 생성 입력의 클립은 Draft·Final 사이에서 재사용; 최종 출력 크기 변경 자체로 재생성하지 않음 |
| 편집으로 복구 | 원본에 충분한 여유 구간이 있으면 시작점을 옮겨 재편집; 새 생성 없이 다시 QC |
| 중복 요청 방지 | 같은 생성 입력의 작업 ID와 접수 상태 보존; 불확실한 접수는 복구 전 추가 제출 중단 |
| 별도 예산 | USD와 OpenArt credits를 합산하거나 임의 환산하지 않음 |

단순히 싼 모델이라는 이유로 원하는 스타일·동작에 적합하다고 가정하지 않는다. 첫 3샷 비교 후 사용할 후보만 등록한다. `shots` 정책으로 특정 장면의 후보와 순서를 명시할 수 있다. LOCK된 manifest의 명시적 provider/model 지정도 준수한다.

재시도에는 실패에 대한 실제 근거가 들어간다. 대기·검토 미완료·API 키 누락·네트워크 불확실성은 모델을 바꾸는 이유가 아니다. 설정한 횟수를 소진하면 해당 장면을 멈춘다. 반복 RESUME으로 새 유료 시도를 만들지 않는다.

## 후보 등록

기존 프로젝트에 다음 명령을 실행하면 기존 설정 파일을 찾아 정책을 만든다. 이 명령은 견적 조회·승인·결제를 하지 않는다.

```bash
python -m engine.cli economy-init projects/project_001
```

`render/economy.json` 예시다. 경로가 가리키는 설정에는 실제 조회한 form·참조·견적이 있어야 한다.

```json
{
  "version": 1,
  "provider_order": ["openart", "fal"],
  "same_model_retries": 1,
  "profiles": [
    {"id": "primary", "provider": "openart", "config_path": "render/profiles/primary.json"},
    {"id": "alternate", "provider": "openart", "config_path": "render/profiles/alternate.json"},
    {"id": "wan", "provider": "fal", "config_path": "render/fal_config.json"}
  ],
  "shots": {"S003": ["primary", "wan"]}
}
```

OpenArt의 여러 모델은 `register_quote(..., profile="primary")`처럼 각각 저장한다. Draft와 Final에 같은 모델·설정을 등록하면 보관한 원본을 재사용한다. 생성 해상도·모델·지시·참조가 바뀌면 다른 생성 입력으로 취급한다. 단순 견적 만료시간 갱신은 새 입력이 아니다. 가격이 변경되면 재견적·승인 대상이다.

fal은 실행 환경의 `FAL_KEY`가 필요하다. 키는 프로젝트 설정이나 Git에 저장하지 않는다. 지원 범위는 기존 Wan 2.2 Turbo adapter이며, 문서에 없는 API나 구독의 무제한 사용권을 가정하지 않는다.

## Work 실행 순서

1. 음원·Bible·실제 콘티를 준비하고 사용자 LOCK을 받는다.
2. 후보별 실제 설정과 견적을 등록한다. Control Panel에서 **Economy**를 선택하거나 다음 명령으로 첫 생성과 최대 재시도 경로를 확인한다.

```bash
python -m engine.cli estimate projects/project_001 --seconds 30 --renderer economy --quality draft
```

3. 사용자가 구체적인 배치를 승인하면 해당 `estimate_id`를 승인 기록에 저장한다. USD와 credits의 상한을 각각 확인한다. 정해지지 않은 구독·충전은 수행하지 않는다.
4. 실행하고 작업 목록을 확인한다.

```bash
python -m engine.cli compile projects/project_001 --seconds 30 --renderer economy --quality draft
python -m engine.cli work-status projects/project_001
```

5. OpenArt `CLAIM_THEN_SUBMIT_OPENART_ONCE` 작업은 제출 직전 정확한 설정의 비용을 재확인한다. 변경됐으면 다시 견적을 만든다. 승인된 동일 비용이면 `claim-job PROJECT JOB_ID`로 SUBMITTING을 기록한 뒤 외부 도구에 한 번 제출하고 `record_submission`으로 반환된 history ID를 즉시 기록한다. 결과 카드가 대기를 요구하면 해당 도구 지침에 따라 다음 재개 시점에 기존 결과를 가져온다.
6. `REVIEW_ANIMATION`에서는 원본·편집 영상의 실제 동작과 추출 프레임, 참조와 Bible을 비교한다. 관찰하지 않은 항목의 점수를 채우지 않는다. `save_review`로 근거를 기록한 뒤 RESUME하면 통과 컷은 편집에 사용하고 실패 컷만 정해진 경로로 재시도한다.
7. 모두 통과하면 FFmpeg가 원곡에 맞춰 MP4와 파생 파일을 출력한다. 곡 전체로 확장할 때도 같은 manifest를 사용한다.

## 생성 없이 편집 수정

예를 들어 5초 원본에서 4초를 사용한다면 뒤쪽의 더 좋은 4초를 선택할 수 있다. 부족한 길이를 정지 프레임이나 속도 변경으로 채우지 않는다.

```bash
python -m engine.cli edit-take projects/project_001 S001 --source-in-ms 500 --notes "첫 0.5초의 손 형태 오류를 제외하고 뒤쪽 동작 사용"
```

다음 compile에서 편집을 적용하고 결과가 달라지면 새 QC를 요구한다. 기존 QC는 정확한 영상 바이트와 제작 LOCK에 연결돼 있다. 원본이 바뀌었으면 이전 편집 지시는 자동 적용하지 않는다. Control Panel의 QC 화면에도 같은 편집 기능이 있다.

## 저장 상태와 한계

- `render/takes/`: 보관한 생성 원본과 해시·제공자·작업 ID.
- `render/progress/`: 같은 생성 계획에서 선택한 attempt와 수정 근거. 출력 크기를 바꿔도 이미 버린 컷을 처음부터 다시 선택하지 않는다.
- `render/edit_decisions.json`: 원본 해시에 연결된 구간 선택.
- `render/requests/`, `responses/`, `fal_jobs/`: 접수·다운로드·실패 근거. SUBMITTING에서 중단되면 이력 대조가 우선이다. 파일을 지워 새 요청으로 만들지 않는다.
- `qc/report.json`: 현재 컷 상태와 실제 검토용 경로. `work-status`는 다음 조치를 읽기 전용으로 보여준다.

최대 예상 비용은 모든 승인된 시도를 포함하는 보수적 상한이며, 캐시에서 재사용할 컷도 포함한다. 실제 신규 예약은 저장된 원본을 재사용할 때 발생하지 않는다. 같은 작업의 기존 예약을 중복 합산하지 않는다. 공급자 청구서가 아닌 **이 프로젝트의 예약 장부**이므로 별도 웹 사용·월 구독료·세금·이미지 생성비는 추적하지 않는다. 월 50달러 전체 지출 관리에는 이 항목들까지 고려해야 한다.

로컬 출력 1080p는 생성 원본을 확대할 수 있다. 별도 AI 업스케일·프레임 보간·캐릭터 리깅·자동 립싱크 비용이나 품질을 포함하지 않는다. 자동화가 실패 컷의 낭비와 수작업 편집을 줄이도록 구현했으며, 완성 MV 가격을 보장하지 않는다.
