# FRAME_ANIMATION_V1 개발 시작 안내

기존 구현 검토 기준: [main 1f5684a8f19893d8f83f487cf329bb2425eeb25b](https://github.com/BeautifulMind-JT/film-unit-mv-studio/tree/1f5684a8f19893d8f83f487cf329bb2425eeb25b)  
대상: Film Unit v0.4 프레임 애니메이션 제작 모드; 저장·실행·인코더 선택  
정본 설계: [상세 설계](FRAME_ANIMATION_V1_DESIGN_KO.md), [Drive 저장·원격 실행·복수 인코더 성능 설계](FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md)
설계 revision 2 (2026-09-30): 제품 확인 main 41e40478505cf75cf441dd4075c071a0fc462dbf. User의 브라우저 Google 로그인·Drive 스트리밍·AI 구독 실행·복수 인코더·성능 우선 지시를 반영한다.

설계 revision 3: [옵션 A 채택 결정](decisions/FRAME_ANIMATION_V1_ADOPTION_20260930.md)을 개발 범위 근거로 연결했다. ARCHITECTURE.md·PROJECT_SPEC.md는 LEGACY_MV 계약과 새 모드 예외를 구분하며, ANIM-001은 채택한 방향의 상세 ADR을 확정한다. worker credential·token store·cancel/UNKNOWN·명시적 bounded retry는 실행·저장 설계 3절과 5.3절을 따른다.

## 개발 목표와 첫 결과물

외부 이미지 시퀀스·레이어를 정확한 프레임 시간축에서 원곡·가사·전환·자막과 컴파일한다. Drive/local archive, 로컬/원격/구독 runtime, FFmpeg/native/service encoder를 독립 선택하고 원본·최종 PNG·빌드를 보존한다. 기존 LEGACY_MV 프로젝트와 Build 1 replay는 계속 지원한다.

먼저 **ANIM-001의 계약 ADR/명세**에 frame·storage·execution·encoder를 함께 고정한다. ANIM-002~007은 시퀀스 가져오기부터 Preview·Final 후보·보관·재현을 실제 파일로 연결한다. ANIM-008 이후에 native 레이어 제작과 생성 도구를 확장하며, 선행 기능이 생기는 시점부터 ANIM-013~018의 Drive·원격 worker·복수 인코더를 연결한다. 로컬 기준선은 정확성 비교용이며 최종 성능 모드를 PC에 고정하지 않는다. 실제 작품은 어려운 본편 컷 W00을 먼저 제작해 경로를 판단한다.

이 변경은 개발 기준 문서다. 아래 모듈·CLI·데이터 버전은 구현 대상이며 아직 제품에 설치된 기능이 아니다. 코드 구현, 런타임 활성화, 유료 요청, 작품 제작·최종 출력 승인은 각각 해당 범위의 근거를 갖춘다.

## 읽을 문서

1. 저장소 [AGENTS.md](../AGENTS.md), [README.md](../README.md), [ARCHITECTURE.md](../ARCHITECTURE.md), [PROJECT_SPEC.md](../PROJECT_SPEC.md).
2. [상세 설계](FRAME_ANIMATION_V1_DESIGN_KO.md) 3~8절: 현재 구현의 충돌, 시간축, 자산·동작·노출·제작 경로.
3. [실행·저장 성능 설계](FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md) 1~9절: Drive archive·workspace·FrameStream·worker·encoder·구독·scheduler·seal.
4. 상세 설계 9~13·15~18절 및 실행·저장 설계 10~11절: LOCK·검수·비용·모듈·티켓·수용 테스트.
5. [공유 정책 포인터](CONTROL_PLANE_POINTER.md)와 실제 착수 시 사용할 [TASKS/TEMPLATE.md](../TASKS/TEMPLATE.md).
6. [전송·pack·완료 근거 고도화 후보](FRAME_ANIMATION_V1_EVOLUTION_KO.md): transfer edge·seekable pack·bounded restore·독립 완료 facets. 선행 #19/#20 위의 DESIGN_ONLY 후보이며 채택 후 승인 plan commit에 pin한다.

ANIM-001~018은 기존 개발 항목이며 이번 후속 후보는 ANIM-019~023을 추가한다. 중앙 control-plane의 TASK_ID, 실행 소유자 또는 dispatch 상태를 자동 생성한 것이 아니다. 실제 구현 작업의 canonical GitHub task는 이 명세의 해당 절을 링크하고, mutable 실행 상태는 issue/PR/control record에 둔다. 기존 18개 ID·범위·선행 관계는 유지하며 기존 bootstrap의 완료 기록에 새 수용 범위를 소급하지 않는다. 새 승인 plan commit의 개발 완료 분모는 23이고, 전체 개발 closeout은 023이 001~022 전부의 delivery를 확인한다.

## ANIM-001: 계약 ADR/명세

### 범위

코드 구현에 앞서 새 모드의 읽기·쓰기 정본, 버전, 타입, 승인 binding을 명시한다. 현재 함수에 새 데이터를 넣어 기존 검증을 우회하는 방식으로 시작하지 않는다.

| 계약 | 확정할 내용 |
|---|---|
| 모드·버전 | LEGACY_MV / FRAME_ANIMATION_V1 분기; Project 4·Shot 3·Build 2 및 새 animation schema 제안의 소비자·지원 버전 |
| 시간축 | timeline/edit.json 정본; 0-based 정수 프레임·끝 제외 구간; ms는 읽기용 파생 view |
| 편집·전환 | 원본 사용 구간, 편집 순서, pairwise overlap, 출력 길이와 전환 weight 규칙; v1의 세 컷 중첩 거부 |
| 샷·자산 | STATIC/ANIMATED 동작 의도, 구간별 A/B/C 경로, 결과 자산 종류 분리; FRAME_SEQUENCE / COMPOSITE_SEQUENCE |
| 제어·노출 | MotionPlan 사건·keypose·breakdown·contact·occlusion; 레이어별 ones/twos 및 빈 슬롯·끝점 처리 |
| 원본·RGBA | asset ID·revision·파일 hash·provenance; alpha·crop origin·pivot·coordinate space 보존 |
| 검수·LOCK | PLAN/WAVE/FINAL 범위; 컷·전환·실제 출력 approval; content digest와 상태 digest 분리 |
| 빌드·replay | Build 2 archive·frame_map·PNG·encode receipt; LOCAL_FULL offline / DRIVE_BOUNDED online·restore; Build 1 호환 |
| 저장·작업 공간 | browser OAuth·object/member hash·immutable revision·PC/worker disk·RAM·VRAM·bounded cache |
| 실행·인코더 | ExecutionPlan·FrameStream·capability·job identity/UNKNOWN·복수 driver·mux·독립 verifier·품질·entitlement·예산 |
| 전송·pack 후보 | coordinator relay의 실제 위치/edge·resource reservation, seekable pack offset/length/member hash·range/full 검증·fallback·restore |
| 완료 증거 후보 | host-pinned merged 개발 node와 qualification/acceptance/release의 독립 facets·필요 scope·현재 evidence binding |

### 지켜야 할 조건

- 컴파일은 고정 FramePlan을 선택된 로컬/원격/구독 실행 환경에서 처리한다. creative generation을 임의 실행하지 않고 remote compose/encode는 실행 계획·사용 권한·비용 범위로 관리한다.
- 저장 위치·실행 위치·인코더를 분리한다. FFmpeg 밖의 native encode가 실제로 동작해야 복수 driver 완료로 집계한다.
- 성능은 같은 품질의 전체 완료 시간·PC/worker peak 공간·전송·사용량으로 비교한다. GPU 광고 fps만으로 가장 빠른 경로를 선택하지 않는다.
- 원곡 파일·원문 가사·검토된 cue는 컷과 전환 변경으로 이동하거나 재작성하지 않는다.
- 기존 프로젝트를 열었다는 이유로 새 모드로 바꾸거나 기존 검수·LOCK을 새 계약에 자동 승계하지 않는다.
- 움직이는 샷을 정지 이미지로 자동 대체하지 않는다. 로컬 제작도 사람의 동작 검수가 필요하다.
- provider가 끝점·포즈·mask·참조 입력을 지원하지 않으면 필요한 조건을 버리고 실행하지 않는다.
- paid quote·예약·UNKNOWN fencing·중복 제출 방지는 기존 계약을 유지한다.
- W00은 실제 본편의 제작 순서다. 고정된 별도 pilot이나 합성 회귀 fixture로 작품 적합성을 대신하지 않는다.
- 완료 빌드는 보존한다. 실제 출력 승인은 sealed build를 대상으로 별도 기록한다.

### 완료 증거

1. ADR/명세가 두 정본 설계와 기존 제품 계약 사이의 변경 범위·migration·버전 소비자를 명시한다. ARCHITECTURE.md·PROJECT_SPEC.md·채택 결정 문서를 명시적 소유 경로로 포함하며, 이미 채택한 방향과 추가 consequential decision을 구분한다. 저장/worker/encoder 선택·완료 보관·online/offline replay·구독 사용권·공간/예산 경계, OAuth client 책임·worker 중개 전송·token store 차단, cancel 경합·UNKNOWN fencing·명시적 bounded retry를 포함한다.
2. 96+96−12=180프레임과 출력 파일 90번의 두 원본 대응을 같은 인덱스 규칙으로 설명한다. 내부 output frame 89는 S001 frame 89와 S002 frame 5에 대응한다.
3. 240초·24fps 작품에서 원본 사용 합계−전환 overlap 합계=5,760을 만족하도록 예시를 제시한다. 60×96프레임에 overlap 144를 추가하면 5,616프레임이므로 원본 길이/여유분을 다시 계획해야 한다.
4. 빈 노출·부족한 소스·미검수 시퀀스·stale 승인·세 컷 중첩을 차단할 수용 조건과 legacy 보존 조건이 있다.
5. 아키텍처/승인 의미를 변경하는 계약은 저장소의 A3·non-author 검토와 User 결정 근거에 연결한다. 문서 작성자의 자체 검증을 독립 감사 PASS로 기록하지 않는다.
6. 고도화 후보를 채택한 task는 고도화 설계 1~5절의 schema/소비자·경로 eligibility·index bounds·검증/확정 순서·실제 완료 근거를 ADR에 고정한다. worker 직접 Drive 인증은 별도 ADR/사용자 결정/qualification 전에는 기본 후보에서 제외한다. 설계 채택이 중앙 구현·host qualification이나 제품 자격을 대신하지 않는다.

## 구현 묶음과 의존성

| 항목 | 선행 | 구현 범위 | 완료 기준의 핵심 |
|---|---|---|---|
| ANIM-001 | 기존 소스·설계 | 계약 ADR/명세 | frame·자산·승인·storage/execution/encoder 정본·버전 |
| ANIM-002 | 001 | 명시적 migration·backup·legacy reader | 원음·가사·과거 빌드 보존, 새 승인 자동 승계 없음 |
| ANIM-003 | 001, 002 | index/폴더 import·version·hash·resolver | 누락·손상·alpha 유실 차단, draft Preview |
| ANIM-004 | 003 | frame clock·노출·시퀀스 정규화 | 슬롯 전체 coverage·24fps·끝점 중복 없음 |
| ANIM-005 | 004 | pairwise transition·frame_map | 180프레임 예제, 다중 원본·weight 추적 |
| ANIM-006 | 005 | 컷/전환 검수·출력·inventory·replay | 미검수 Final 차단, 보관 입력으로 재현 |
| ANIM-007 | 006 | PLAN/WAVE/FINAL LOCK·W00·경로 결정 | 선택한 본편 컷만 먼저 제작, checkpoint 정지 |
| ANIM-008 | 007 | native C 계층·pivot·대체 그림·mask | 피사체 동작과 카메라 이동을 별도로 재생 |
| ANIM-009 | 007 | A 제어 이미지·packet·수동 hand-off | 역할·시점·참조를 확인한 draft import |
| ANIM-010 | 008, 009 | capability·구간·quote·UNKNOWN·PTS adapter | fake provider로 필수 입력·중복 제출 차단 |
| ANIM-011 | 007, 008, 009 | 기존 UI 연결, 010 미병합 기능은 비활성으로 둠 | 한 컷 준비→검토→수정→출력·승인; 늦은 adapter 연결은 012 책임 |
| ANIM-012 | 010, 011 | 240초 통합 회귀·늦은 B adapter UI 연결·desktop packaging·문서 | 두 모드 회귀·fake adapter UI 수용·각 OS 패키지 검사 |
| ANIM-013 | 001, 003, 006 | browser Drive OAuth·archive·bounded cache·restore | 멤버/원본 보존·공간 상한·접근/끊김·복원 |
| ANIM-014 | 001, 004, 007, 013 | FrameStream·ExecutionPlan·worker·receipt·UNKNOWN | 같은 frame 계약의 실제 remote·stale/중복 fence |
| ANIM-015 | 001, 004, 006 | encode/mux/verify 분리·복수 driver | FFmpeg와 독립 native encode의 실제 품질·PTS·원음 |
| ANIM-016 | 009, 014, 015 | 구독 probe·사용권·packet/notebook·import | 실제 서비스의 도구·한도·결과·자동/수동 구분 |
| ANIM-017 | 005, 013~016 | 부분 재컴파일·parallel·prefetch·zero-copy 후보 | 같은 품질 end-to-end cold/warm·공간·전송·복구 |
| ANIM-018 | 008, 012~017 | User-only actual240초·1080p·Drive→worker→archive·UI qualification | 실제 허용 route/source/artifact의 실행·verification·restore·실패/성능 evidence와 User 승인 전 merge/DONE 금지; 부재면 WAITING |
| ANIM-019 | 001, 013~016 | capability evidence registry·scope eligibility·갱신/측정 | session/계정/도구/사용권 변경 차단·현재 scope만 허용 |
| ANIM-020 | 003~006, 017, 019 | canonical render manifest·invalidate closure·부분 재현 | cold/warm 동일 범위·비영향 보존·clean/subbed/encode 승인 분리 |
| ANIM-021 | 013~015, 019 | durable worker journal·업로드 checkpoint·archive commit/seal | submit/publication 응답 손실·crash·취소 경합·중복/UNKNOWN fence |
| ANIM-022 | 007, 010~012, 019~021 | W00 경로 판단·전체 재생 검토 UI/기록 | 본편 내 어려운 컷·KEEP/CHANGE/MIX·current 실제 파일 승인 |
| ANIM-023 | 001~022 전부 | 전체 개발 delivery 및 목표 qualification/acceptance closeout | 분모·증거 누락/변경·실제 환경/작품/릴리스 미완료를 정확히 표시 |

모듈·UI·선택 CLI 후보는 상세 설계 14~15절과 실행·저장 설계 10절을 따른다. 한 티켓의 acceptance criteria는 상세 설계 16~17절과 실행·저장 설계 10~11절의 해당 범위를 참조한다. 의존성은 계약을 고정하기 위한 권장 순서이며, 여러 writer를 자동으로 dispatch하는 지시가 아니다.

## ANIM-012: 늦게 병합된 B adapter의 UI 통합 수용

ANIM-011은 ANIM-010보다 먼저 병합될 수 있다. 이때 adapter 연결 지점을 비활성으로 둔 것은 011의 허용된 중간 결과이며, 이후 adapter가 생겨도 비활성 상태를 최종 통합 완료로 남기지 않는다. **ANIM-012가 010과 011의 병합 결과를 같은 source HEAD에서 연결하고 검사한다.** 기존 DAG와 18개 node ID는 유지한다.

012의 산출물은 기존 앱에서 B 경로의 제어 입력·capability·quote/예약·요청·상태·결과 검증·draft import를 adapter에 연결하는 구현과 UI 수용 증거다. 실제 provider 호출이나 유료 생성은 하지 않으며, 명시적인 개발/시험용 fake adapter와 합성 입력을 사용한다. fixture에서 구성한 LOCK·quote·승인·예약은 엔진 경계 검사이며 실제 작품·지출·provider qualification 승인으로 기록하지 않는다.

| fixture | 앱에서 수행할 동작 | 필요한 증거 |
|---|---|---|
| `UI-B-READY` | 고정 입력과 scope에 맞는 fake capability/quote로 요청하고 반환 시퀀스를 draft로 가져옴 | 연결 지점이 활성화되고 job/attempt·입력 hash·quote binding이 유지됨; 반환 bytes/시점 검증 후 draft import; 외부 provider 요청 0 |
| `UI-B-INPUT-QUOTE` | 필수 ref/pose/end 입력을 누락하거나 quote 후 입력/가격/권한 binding을 변경함 | 원인을 UI에 표시하고 제출 전에 차단; fake submit 횟수 0; 참조를 무단으로 자르거나 새 quote를 승인으로 간주하지 않음 |
| `UI-B-UNKNOWN` | fake가 접수 뒤 응답을 잃게 하고 동일 입력으로 다시 실행하려 함 | 기존 identity의 UNKNOWN을 표시하고 새 job/attempt·대체 제출·예약 해제를 차단; 명시적 동일 작업 확인만 수행하고 자동 polling/retry 없음 |
| `UI-B-CANCEL-RACE` | fake에서 취소 요청 후 완료/취소 확인 순서를 각각 바꿈 | 요청과 확인을 구분; 완료가 먼저 확정되면 같은 identity 결과를 검증하고 draft로 취급; 종료 불명 중 예약·대체 제출을 fence하고 취소를 환불로 표시하지 않음 |

해당 증거는 source HEAD·fixture/input digest·fake adapter version·동작과 관측 결과·실제 실행 명령에 연결한다. 늦은 연결을 기존 011의 완료 기록에 덧씌우지 않고 012 delivery의 변경/수용 범위로 기록한다. 이 UI 통합과 기존 240초/두 모드/세 OS 패키지 조건을 함께 충족해야 012의 개발 수용을 완료할 수 있다. 실제 B provider의 사용 가능 여부·작품 채택·예술적 승인·release는 별도 근거를 요구한다.

## 첫 기능 묶음의 검증

ANIM-003~007은 실제 원본 그림과 파일을 사용하는 기능 단위로 검증한다.

- RGBA·index·멤버 hash를 보존하고, 누락·변조·허용되지 않은 경로를 차단한다.
- 레이어별 ones/twos·홀수 끝점·공유 anchor에서 빈 슬롯과 중복을 탐지한다.
- 96+96−12=180 및 240초 출력의 디코드 프레임 수·PTS·24/1 fps·전환 대응을 검사한다.
- 한 컷 교체 시 다른 source/recipe·원곡 hash·가사 cue를 유지하고 필요한 검수만 stale로 만든다.
- 새 모드에서 미검수·부족한 길이·stale approval·범위 밖 production을 차단한다.
- 라이브 프로젝트를 숨긴 상태에서 Build 2를 replay하고 inventory 변조를 탐지한다.
- 기존 전체 pytest와 Build 1 replay, 현재 Preview/Final 의미를 회귀 검사한다.

구현 PR은 실제 실행한 명령·결과·현재 HEAD의 CI를 기록한다. 이 문서에 정의된 새 테스트는 아직 실행한 증거가 아니다. CI의 합성 4분 fixture와 실제 W00·최종 작품 검토는 별도로 기록한다.

## Drive·원격·복수 인코더 수용

ANIM-013~018은 실행·저장 설계 11절을 따른다. 실제 Drive/member hash·cache/spool 상한·PC/worker 공간, job UNKNOWN/stale/중복·seal crash, 독립 native encode·frame/PTS/원음/품질, 실제 구독의 검증된 범위, 고정 archive online replay/offline restore를 확인한다. 권한·GPU·도구가 없으면 fake 성공과 실제 qualification 미완료를 분리한다.

성능 보고는 동일 input/quality의 cold/warm end-to-end 시간, stage timeline, peak PC/worker disk·RAM·VRAM, 전송·cache hit·사용량/비용·중단 후 복구를 포함한다. CLI를 사용하지 않는 browser/app/notebook 사용자 흐름을 실제 검증한다.

고도화 후보의 ANIM-013/014/017/018은 고도화 설계 6~7절의 ROUTE/PACK/RESTORE/FACETS fixture를 해당 범위에 구현한다. 기본 relay의 PC WAN 이동·공유 링크 경합까지 보고하고, 한 컷 sparse 변경은 필요 member/halo의 실제 read/request/decode 증폭을 확인한다. Range 미지원은 명시적으로 사전 예약한 whole-pack fallback 또는 차단으로 처리한다. 부분 멤버 검증으로 full-pack 검증/완료를 기록하지 않는다.

개발 결과의 `node_state=DONE`은 중앙의 host-pinned merged delivery 근거를 요구한다. `qualification_state=NOT_REQUIRED/UNQUALIFIED/PARTIAL/QUALIFIED`, `acceptance_state=NOT_REQUIRED/PENDING/ACCEPTED/REJECTED`, `release_state=NOT_AUTHORIZED/NOT_RELEASED/RELEASED`는 별도 현재 scope 근거로 보고한다. 필요한 실제 환경이 없으면 UNQUALIFIED/PENDING으로 남기며 NOT_REQUIRED로 면제하지 않는다. ANIM-018은 user_merge=true/astra_auto_merge=false인 실제 qualification milestone이다. 실제 허용된 한 경로의240초 통합·archive/replay/restore·UI·실패 및 고도화 RELAY-AUTH/TRANSPORT-RETRY/ARCHIVE-LEVEL evidence를 exact implementation/source HEAD·input/recipe/profile·device/toolchain·route/epoch·artifact hashes·시각에 pin하고, User의 exact evidence 승인 뒤에만018 merge/DONE을 허용한다. code/fake PASS나 이전 개발 merge는 대신하지 못하며 환경/권한/evidence 부재면018 WAITING이다. 작품 사람 승인과 release는 계속 별도다.

## ANIM-019~023: 후속 개발 범위와 수용

기존 항목은 renderer/worker/transport의 첫 동작을 제공하고, 아래 후속 항목은 그 결과를 현재 자격·재현·중단 복구·작품 검수와 전체 집계로 연결한다. 같은 구현을 두 번 완료로 집계하지 않는다. 각 항목은 현재 predecessor delivery HEAD·새 task scope·실제 검사 명령을 별도로 기록한다. A3 항목은 capability/권한 또는 외부 side effect 경계 변경이고, A2 항목도 current-head 독립 검토를 요구한다.

| 항목/감사 | 산출물·소유자 | 필수 수용 fixture |
|---|---|---|
| 019 / A3 | registry·preflight/scheduler/UI 상태; builder 단일 writer, Fable 독립 설계·코드 감사; 실제 권한/자원은 User/배포 책임자 | `CAP-SCOPE`: CPU≠encode·CUDA≠NVENC·LLM구독≠compute·Drive connector≠network; `CAP-STALE`: session/driver/계정/권한/한도 변경; `CAP-MEASURE`: cold/warm·수동 시간·edge 경합 누락 표시 |
| 020 / A3 · User merge | schema/canonicalizer·dependency graph·manifest·selective rebuild; renderer/toolchain 검증은 독립 MediaVerifier | `REBUILD-CLOSURE`: 그림/전환/font/cue/encoder/locator/미채택 후보 변경; `REBUILD-COLD-WARM`: 동일 입력/품질·실측 범위·비영향 hash·원곡/가사 보존; `MANIFEST-CYCLE`: 자기 digest/approval·NaN/중복 key 거부 |
| 021 / A3 | journal·submit/attach·transfer checkpoint·commit/seal 복구; coordinator 단일 상태 writer, provider callback은 관측만 | `JOB-CRASH`: intent/응답/검증 사이 crash; `JOB-CANCEL`: 늦은 완료·취소 불명; `UPLOAD-RESUME`: server offset≠archive verification; `SEAL-CRASH`: 객체/manifest 공개 전후 응답 손실·다중 후보·손상; 새 compute 제출 0·원본/과거 seal 보존 |
| 022 / A2 MILESTONE | W00·wave/전체 재생/issue/승인 UI·기록; 실제 작품 판단은 박준태 또는 명시적 위임자 | `W00-ROUTE`: 본편 컷 KEEP/CHANGE/MIX 후 closure/quote/UNKNOWN 적용; `ART-STALE`: 컷/전환/자막/encode 변경 뒤 stale 범위; 실제 작품 검토 부재 fixture는 PENDING 유지, 합성 검사로 사람 승인 자동 생성 0 |
| 023 / A2 MILESTONE | 전체 23-node closeout view·근거 표·문서, source/plan digest별 read binding; host completion facets 구현 전에는 draft 표 | `CLOSEOUT-ALL`: 22개 중 하나라도 missing/stale면 전체 개발 delivery 미완료; `CLOSEOUT-SCOPE`: 018 통합 이후 새 계약/환경 변경이 기존 qualification 승계 못함; `CLOSEOUT-ART`: 실제 runtime/W00/작품 승인·release 부재 상태 정확히 유지 |

019~022의 fake fixture는 개발 수용을 검증한다. 실제 Drive/remote/native/subscription probe, 실제 W00 채택본·최종 음악영상 전체 재생은 해당 환경·사용권·작품 승인 범위가 있을 때만 검증한다. 개발 코드가 merged되어도 필요한 실제 qualification/acceptance가 없으면 제품 목표 READY/FINAL_APPROVED를 선언하지 않는다. 023은 개발 delivery complete와 product-qualified/작품 accepted/release authorized를 별도 보고하며, 보고 도구의 개발 DONE이 missing facet를 해제하지 않는다. 실제 환경을 기다리는 일을 명시적 WAITING/UNQUALIFIED/PENDING으로 남기며 NOT_REQUIRED로 면제하거나 User 승인을 builder가 대리 기록하지 않는다.

018 실제 qualification이 없으면023의 전체 delivery closeout도 WAITING이며 missing018을 분모에서 빼거나 code-only018 DONE으로 바꾸지 않는다. 앞선013~017의 허용된 code-only 개발 delivery는 별도 qualification 미완료를 표시하며 계속 준비할 수 있다.

분모 23은 승인된 이 plan revision의 작업 노드 수이며018은 실제 qualification을 요구한다. qualification과 작품 검수·release는 단순 완료 node 수로 합산하지 않으며, 018 baseline 수용만으로 019~022의 새 scope나 최종 023을 닫지 않는다. 기존 plan 실행 중이라면 중앙의 보호된 plan 개정/승인 절차로 새 revision을 채택하기 전 이 JSON을 live registration에 덮어쓰지 않는다.

## 이번 설계의 실행 경계

문서 반영은 실제 remote service 배포·credential 발급·추가 과금·무승인 생성·control-plane runtime 활성화·consumer UI 자동 조작을 수행하지 않는다. 외부 worker/API·notebook은 사용자가 허용한 연결·권한·사용 범위 안에서 구현한다. 240초·24fps·1080p·16:9는 대상 작품의 출력 프로필이며 기존 모든 프로젝트의 기본값을 강제로 바꾸지 않는다.
