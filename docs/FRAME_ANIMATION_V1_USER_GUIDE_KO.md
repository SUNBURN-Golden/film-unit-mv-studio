# FRAME_ANIMATION_V1 사용 안내 (개발판)

`FRAME_ANIMATION_V1`은 외부에서 만든 프레임·레이어를 정확한 정수 프레임
시간축(24/1)에서 원곡·가사·전환과 컴파일하는 새 제작 모드다.
`LEGACY_MV` 프로젝트는 아무것도 바뀌지 않고, 두 모드는 섞이지 않는다.
기존 프로젝트가 자동으로 새 모드로 바뀌지 않으며 `animation-init`은
LEGACY_MV 프로젝트를 사람이 직접 실행할 때만 변환한다.

이 문서는 **개발·시험 상태**의 기능을 설명한다. 아래의 모든 결과물은
DRAFT이며, 이 모드를 실행하는 것만으로 실제 작품 승인·출력 권한이
생기지 않는다.

## 전체 흐름

편집 화면의 `08 · ANIMATION` 탭은 `production_profile == FRAME_ANIMATION_V1`
프로젝트에서만 보인다. 한 컷의 흐름은 섹션 번호 순서를 따른다.

1. **01 · 준비** — 자산(시퀀스·레이어·그림)을 가져오고 샷 계획을 저장하고,
   작업 패킷과 총 프레임 수 검산을 확인한다.
2. **02 · 동작·노출** — 계획의 경로·keypose·노출 요약을 보고 경로 C 레이어의
   교체 그림을 넣는다.
3. **03 · Import** — 프레임 시퀀스(경로 A 결과)를 가져오거나 draft 프레임을
   조립한다.
4. **04 · 검토** — 컷/전체 Draft Preview를 재생하고 현재 검수를 기록한다.
5. **05 · 수정** — 한 컷의 프레임·그림 교체. 교체한 컷에 묶인 검수·LOCK은
   낡은 것으로 표시되고 재검수 범위가 안내된다.
6. **06 · 출력·승인** — wave 선언 → LOCK → 경로 결정 → Final 후보 빌드 →
   최종 승인 기록.

## 세 제작 경로

샷 계획(`plan.json`)의 각 구간은 `path` 하나를 선언한다.

| 경로 | 하는 일 | 입력 |
|---|---|---|
| **A** | 이미지 기반 프레임 제작. 외부 도구로 만든 PNG 시퀀스를 `index.json`(순서·해시 명시) 또는 폴더(파일명 사전순)로 가져와 샷에 핀한다. | keypose·breakdown·pose·layout 등 조건 입력은 작업 패킷으로 전달 |
| **B** | 구간 생성 adapter에 조건 입력을 넘겨 클립을 받는다. **현재 연결된 것은 개발·시험용 `fake_segment`(UNQUALIFIED)뿐**이다. | keypose·breakdown·pose·layout PNG(필수 입력은 adapter capability가 정한다) |
| **C** | 컴포지터 경로. `RIG_SPEC` 핀과 레이어 트랙(LAYER_RGBA: 알파·크롭 원점·pivot·z_order 보존)을 바인딩한다. | 레이어 자산 + RIG_SPEC |

### 경로 B 실제 절차(개발·시험용)

`경로 B · segment 생성(개발·시험)` 섹션은 `fake_segment` adapter만 연결한다.
실제 provider adapter는 declaration-only로 남아 있어 호출이 거절된다.

1. 저장된 계획의 경로 B 구간을 고르고, 조건 입력 PNG를 올려
   `import_segment_control`로 기록한다(입력 digest가 journal에 남는다).
2. **견적** — capability preflight + quote. 필수 입력이 빠지거나 견적 후
   입력이 바뀌면(stale quote) 제출 전에 차단된다.
3. **비용 승인(별도 사람)** — 승인자·승인 크레딧 상한·동의 체크가 견적과
   별도 단계로 필요하다.
4. **제출** — 승인된 견적으로 예약 + submit. job journal에 입력 digest·
   adapter 버전·동작이 기록된다.
5. **상태 확인·조정** — `reconcile`은 사용자가 버튼으로만 실행한다. 자동
   폴링·자동 재시도·대체 제출은 없다. 결과가 불명(UNKNOWN)이면 같은
   identity의 조정만 허용되며 새 job/attempt 제출과 예약 해제가 막힌다.
6. **취소** — 취소 요청(CANCEL_REQUESTED)과 완료가 경합할 수 있다.
   취소 확인 전 결과물이 먼저 완성되면 결과를 보존하고, 취소 확인이
   불명이면 UNKNOWN으로 fence한다.
7. **결과 검증 → draft import → 컷 반영** — 반환 클립을 검증한 뒤에만
   DRAFT로 import하고 컷의 시퀀스에 반영한다.

모든 단계의 시간·이벤트·attempt/request/operation ID·adapter 라벨은
`job journal` expander에서 확인할 수 있다.

## 무엇이 fake이고 무엇이 UNQUALIFIED인가

- `fake_segment`는 네트워크·유료 호출 없이 로컬에서 결정적 PNG 클립을
  만드는 시험용 adapter다. 그 출력은 `FAKE`·`UNQUALIFIED`로 표시되고,
  실제 provider 준비·품질의 증거로 쓸 수 없다.
- `gemini_video` 등 실제 provider는 선언만 있고 transport가 없어 요청이
  거절된다. 유료 요청·credential 발급·네트워크 호출은 이 모드 어디에도
  없다.
- 가져온 모든 그림·클립·시퀀스는 DRAFT다. 화면은 항상
  `qualification UNQUALIFIED · acceptance PENDING · release NOT_AUTHORIZED`를
  표시한다.
- 예산 예약은 provider 확인 없이 환불된 것으로 취급하지 않는다.
  실패·보류 예약의 해제는 사람의 확인 절차가 필요하다.

## 사람이 정하는 것

자동으로 결정되지 않고 별도 버튼·별도 기록이 필요한 것들:

- 비용 승인(승인자·상한·동의) — 견적과 별도 단계.
- UNKNOWN 상태의 조정(reconcile) — 같은 identity만, 수동 버튼.
- 취소와 그 경합의 확인 — 수동 버튼.
- 컷 검수, wave LOCK, 경로 결정, Final 후보 지정, 최종 승인 —
  각각 정확한 다이제스트에 묶인 별도 protocol 기록.
- 최종 승인은 정확한 봉인 빌드와 현재 검수에만 묶인다. 이 기록도 실제
  작품의 출시 승인이 아니며 release는 계속 `NOT_AUTHORIZED`다.

## 데스크톱 앱

데스크톱 패키지(`desktop/film_unit.spec`)는 `app/*.py` 전체와
`engine` 하위 모듈 전체를 포함하므로 이 화면과 segment 엔진이 함께
배포되고, 얼린 앱의 smoke(`desktop/smoke.py`)가 `app.animation_ui`를
실제로 import한다. Windows·macOS·Linux 실제 패키지 smoke는
`Desktop apps` GitHub Actions 워크플로가 이 PR head에서 실행하며,
실행 기록(run id)은 maintainer가 해당 PR에 기록한다.

## 관련 문서

- [상세 설계](FRAME_ANIMATION_V1_DESIGN_KO.md) — 계약·시간축·경로 정의
- [개발 시작 안내](FRAME_ANIMATION_V1_DEVELOPMENT_KO.md) — 노드별 범위·수용
- [실행·저장 설계](FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md) — Drive·worker·encoder
- [데스크톱 앱 안내](DESKTOP_APPS.md) — 설치·배포·smoke
