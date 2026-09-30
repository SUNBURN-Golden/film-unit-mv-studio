# FRAME_ANIMATION_V1 개발 시작 안내

기준 소스: [main 1f5684a8f19893d8f83f487cf329bb2425eeb25b](https://github.com/BeautifulMind-JT/film-unit-mv-studio/tree/1f5684a8f19893d8f83f487cf329bb2425eeb25b)  
대상: 기존 로컬 Film Unit MV compiler의 v0.4 프레임 애니메이션 제작 모드  
정본 설계: [이미지 기반 애니메이션 적용 검토·개발 설계](FRAME_ANIMATION_V1_DESIGN_KO.md)

## 개발 목표와 첫 결과물

외부에서 제작한 이미지 시퀀스를 가져와, 레이어별 노출·컷·전환·원곡·가사·자막을 정확한 프레임 시간축에서 컴파일하고 원본과 빌드를 보존한다. 기존 LEGACY_MV 프로젝트와 Build 1 replay는 계속 지원한다.

먼저 **ANIM-001의 계약 ADR/명세**를 작성한다. 다음으로 ANIM-002~007을 통해 시퀀스 가져오기부터 전체 Preview, 검수된 Final 후보, 보관·재현까지 실제 파일로 연결한다. ANIM-008 이후에 로컬 레이어 제작과 생성 도구 연결을 확장한다. 실제 작품은 어려운 본편 컷 W00을 먼저 제작해 경로를 판단한다.

이 변경은 개발 기준 문서다. 아래 모듈·CLI·데이터 버전은 구현 대상이며 아직 제품에 설치된 기능이 아니다. 코드 구현, 런타임 활성화, 유료 요청, 작품 제작·최종 출력 승인은 각각 해당 범위의 근거를 갖춘다.

## 읽을 문서

1. 저장소 [AGENTS.md](../AGENTS.md), [README.md](../README.md), [ARCHITECTURE.md](../ARCHITECTURE.md), [PROJECT_SPEC.md](../PROJECT_SPEC.md).
2. [상세 설계](FRAME_ANIMATION_V1_DESIGN_KO.md) 3~8절: 현재 구현의 충돌, 시간축, 자산·동작·노출·제작 경로.
3. 상세 설계 9~13절: 범위 LOCK, 검수, 변경 영향, 빌드·replay, 비용·노동.
4. 상세 설계 15~18절: 모듈 책임, 티켓 완료 기준, 회귀·migration.
5. [공유 정책 포인터](CONTROL_PLANE_POINTER.md)와 실제 착수 시 사용할 [TASKS/TEMPLATE.md](../TASKS/TEMPLATE.md).

ANIM-001~012는 개발 항목 이름이다. 중앙 control-plane의 TASK_ID, 실행 소유자 또는 dispatch 상태를 자동 생성한 것이 아니다. 실제 구현 작업의 canonical GitHub task는 이 명세의 해당 절을 링크하고, mutable 실행 상태는 issue/PR/control record에 둔다.

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
| 빌드·replay | 캡처한 입력 snapshot, Build 2 inventory·frame_map·최종 PNG; Build 1 호환 경로 |

### 지켜야 할 조건

- 컴파일은 로컬 편집·합성·인코딩 작업이다. 외부 생성 호출을 실행하지 않는다.
- 원곡 파일·원문 가사·검토된 cue는 컷과 전환 변경으로 이동하거나 재작성하지 않는다.
- 기존 프로젝트를 열었다는 이유로 새 모드로 바꾸거나 기존 검수·LOCK을 새 계약에 자동 승계하지 않는다.
- 움직이는 샷을 정지 이미지로 자동 대체하지 않는다. 로컬 제작도 사람의 동작 검수가 필요하다.
- provider가 끝점·포즈·mask·참조 입력을 지원하지 않으면 필요한 조건을 버리고 실행하지 않는다.
- paid quote·예약·UNKNOWN fencing·중복 제출 방지는 기존 계약을 유지한다.
- W00은 실제 본편의 제작 순서다. 고정된 별도 pilot이나 합성 회귀 fixture로 작품 적합성을 대신하지 않는다.
- 완료 빌드는 보존한다. 실제 출력 승인은 sealed build를 대상으로 별도 기록한다.

### 완료 증거

1. ADR/명세가 상세 설계와 기존 승인된 계약 사이의 변경 범위·migration·버전 소비자를 명시한다.
2. 96+96−12=180프레임과 출력 파일 90번의 두 원본 대응을 같은 인덱스 규칙으로 설명한다. 내부 output frame 89는 S001 frame 89와 S002 frame 5에 대응한다.
3. 240초·24fps 작품에서 원본 사용 합계−전환 overlap 합계=5,760을 만족하도록 예시를 제시한다. 60×96프레임에 overlap 144를 추가하면 5,616프레임이므로 원본 길이/여유분을 다시 계획해야 한다.
4. 빈 노출·부족한 소스·미검수 시퀀스·stale 승인·세 컷 중첩을 차단할 수용 조건과 legacy 보존 조건이 있다.
5. 아키텍처/승인 의미를 변경하는 계약은 저장소의 A3·non-author 검토와 User 결정 근거에 연결한다. 문서 작성자의 자체 검증을 독립 감사 PASS로 기록하지 않는다.

## 구현 묶음과 의존성

| 항목 | 선행 | 구현 범위 | 완료 기준의 핵심 |
|---|---|---|---|
| ANIM-001 | 기존 소스·설계 | 계약 ADR/명세 | 정본·버전·프레임·자산·승인 계약 |
| ANIM-002 | 001 | 명시적 migration·backup·legacy reader | 원음·가사·과거 빌드 보존, 새 승인 자동 승계 없음 |
| ANIM-003 | 001, 002 | index/폴더 import·version·hash·resolver | 누락·손상·alpha 유실 차단, draft Preview |
| ANIM-004 | 003 | frame clock·노출·시퀀스 정규화 | 슬롯 전체 coverage·24fps·끝점 중복 없음 |
| ANIM-005 | 004 | pairwise transition·frame_map | 180프레임 예제, 다중 원본·weight 추적 |
| ANIM-006 | 005 | 컷/전환 검수·출력·inventory·replay | 미검수 Final 차단, 보관 입력으로 재현 |
| ANIM-007 | 006 | PLAN/WAVE/FINAL LOCK·W00·경로 결정 | 선택한 본편 컷만 먼저 제작, checkpoint 정지 |
| ANIM-008 | 007 | 로컬 C 계층·pivot·대체 그림·mask | 피사체 동작과 카메라 이동을 별도로 재생 |
| ANIM-009 | 007 | A 제어 이미지·packet·수동 hand-off | 역할·시점·참조를 확인한 draft import |
| ANIM-010 | 008, 009 | capability·구간·quote·UNKNOWN·PTS adapter | fake provider로 필수 입력·중복 제출 차단 |
| ANIM-011 | 007, 008, 009 | 기존 UI 연결, 010 기능은 adapter 구현 후 연결 | 한 컷 준비→검토→수정→출력·승인 |
| ANIM-012 | 010, 011 | 240초 통합 회귀·desktop packaging·문서 | 두 모드 회귀와 각 OS의 패키지 검사 |

모듈명과 CLI 후보는 상세 설계 14~15절을 따른다. 한 티켓의 acceptance criteria는 상세 설계 16~17절의 해당 범위를 참조한다. 의존성은 계약을 고정하기 위한 권장 순서이며, 여러 writer를 자동으로 dispatch하는 지시가 아니다.

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

## 개발에 포함하지 않는 작업

이 계획은 새 서버·DB·저장소, 사이트 로그인·브라우저 자동 조작, 자동 유료 생성, control-plane 런타임 활성화, 실제 작품의 미정 입력을 임의 확정하는 작업을 요구하지 않는다. 240초·24fps·1080p·16:9는 대상 작품의 출력 프로필이며 기존 모든 프로젝트의 기본값을 강제로 바꾸지 않는다.

