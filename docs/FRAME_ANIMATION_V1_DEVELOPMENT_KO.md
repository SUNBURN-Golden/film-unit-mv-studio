# FRAME_ANIMATION_V1 개발 시작 안내

기존 구현 검토 기준: [main 1f5684a8f19893d8f83f487cf329bb2425eeb25b](https://github.com/BeautifulMind-JT/film-unit-mv-studio/tree/1f5684a8f19893d8f83f487cf329bb2425eeb25b)  
대상: Film Unit v0.4 프레임 애니메이션 제작 모드; 저장·실행·인코더 선택  
정본 설계: [상세 설계](FRAME_ANIMATION_V1_DESIGN_KO.md), [Drive 저장·원격 실행·복수 인코더 성능 설계](FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md)
설계 revision 2 (2026-09-30): 제품 확인 main 41e40478505cf75cf441dd4075c071a0fc462dbf. User의 브라우저 Google 로그인·Drive 스트리밍·AI 구독 실행·복수 인코더·성능 우선 지시를 반영한다.

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

ANIM-001~018은 개발 항목 이름이다. 중앙 control-plane의 TASK_ID, 실행 소유자 또는 dispatch 상태를 자동 생성한 것이 아니다. 실제 구현 작업의 canonical GitHub task는 이 명세의 해당 절을 링크하고, mutable 실행 상태는 issue/PR/control record에 둔다.

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

1. ADR/명세가 두 정본 설계와 기존 제품 계약 사이의 변경 범위·migration·버전 소비자를 명시한다. 저장/worker/encoder 선택·완료 보관·online/offline replay·구독 사용권·공간/예산 경계를 포함한다.
2. 96+96−12=180프레임과 출력 파일 90번의 두 원본 대응을 같은 인덱스 규칙으로 설명한다. 내부 output frame 89는 S001 frame 89와 S002 frame 5에 대응한다.
3. 240초·24fps 작품에서 원본 사용 합계−전환 overlap 합계=5,760을 만족하도록 예시를 제시한다. 60×96프레임에 overlap 144를 추가하면 5,616프레임이므로 원본 길이/여유분을 다시 계획해야 한다.
4. 빈 노출·부족한 소스·미검수 시퀀스·stale 승인·세 컷 중첩을 차단할 수용 조건과 legacy 보존 조건이 있다.
5. 아키텍처/승인 의미를 변경하는 계약은 저장소의 A3·non-author 검토와 User 결정 근거에 연결한다. 문서 작성자의 자체 검증을 독립 감사 PASS로 기록하지 않는다.

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
| ANIM-011 | 007, 008, 009 | 기존 UI 연결, 010 기능은 adapter 구현 후 연결 | 한 컷 준비→검토→수정→출력·승인 |
| ANIM-012 | 010, 011 | 240초 통합 회귀·desktop packaging·문서 | 두 모드 회귀와 각 OS의 패키지 검사 |
| ANIM-013 | 001, 003, 006 | browser Drive OAuth·archive·bounded cache·restore | 멤버/원본 보존·공간 상한·접근/끊김·복원 |
| ANIM-014 | 001, 004, 007, 013 | FrameStream·ExecutionPlan·worker·receipt·UNKNOWN | 같은 frame 계약의 실제 remote·stale/중복 fence |
| ANIM-015 | 001, 004, 006 | encode/mux/verify 분리·복수 driver | FFmpeg와 독립 native encode의 실제 품질·PTS·원음 |
| ANIM-016 | 009, 014, 015 | 구독 probe·사용권·packet/notebook·import | 실제 서비스의 도구·한도·결과·자동/수동 구분 |
| ANIM-017 | 005, 013~016 | 부분 재컴파일·parallel·prefetch·zero-copy 후보 | 같은 품질 end-to-end cold/warm·공간·전송·복구 |
| ANIM-018 | 008, 012~017 | 240초·1080p·Drive→worker→archive·UI | 실제 실행·restore·실패 주입·성능 보고 |

모듈·UI·선택 CLI 후보는 상세 설계 14~15절과 실행·저장 설계 10절을 따른다. 한 티켓의 acceptance criteria는 상세 설계 16~17절과 실행·저장 설계 10~11절의 해당 범위를 참조한다. 의존성은 계약을 고정하기 위한 권장 순서이며, 여러 writer를 자동으로 dispatch하는 지시가 아니다.

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

## 이번 설계의 실행 경계

문서 반영은 실제 remote service 배포·credential 발급·추가 과금·무승인 생성·control-plane runtime 활성화·consumer UI 자동 조작을 수행하지 않는다. 외부 worker/API·notebook은 사용자가 허용한 연결·권한·사용 범위 안에서 구현한다. 240초·24fps·1080p·16:9는 대상 작품의 출력 프로필이며 기존 모든 프로젝트의 기본값을 강제로 바꾸지 않는다.
