# FRAME_ANIMATION_V1 채택·개발 범위 결정 — 2026-09-30

## 결정과 근거

대표님(박준태)의 직접 지시로 Fable이 제시한 **옵션 A**의 중단된 반영 작업을 이어서 준비한다. FRAME_ANIMATION_V1의 Drive 저장·브라우저 Google 로그인·원격/구독 실행·복수 인코더·성능 우선 방향을 ANIM-001~018 개발의 기준으로 채택한다. 이 결정 기록은 설계·계획 PR에 포함되며 독립 감사 결과나 병합 완료를 대신하지 않는다.

근거는 2026-09-30 사용자 대화다. 사용자 지시의 관련 원문은 다음과 같다.

> CLI나 뭐 이런걸로 안붙이고 그냥 저장소를 단순히 브라우저 등으로 연결하는 형태. 직접 구글로그인 해서 연결하는 형태로

> FFmpeg 말고 다른 인코더도 사용할 수 있게 하고, AI 구독을 통해서 확보되는 서비스로 인코딩하는 부분도 가능하게 만들어줄 것이고, 그렇게 진행되어야겠지. 개발은 어려워도 괜찮아. 고난도라도 최대한 성능 우선으로 설계하면 돼

> 그록봇한테 진행해라잇! 딸깍 하면 지 알아서 완성을 시켜놓는게 목표거든? 그러니까 존나 계획을 크게크게 던져주면 되지 않냐 이말이지. 그렇게 될 수 있게 세팅해

현재 인계 대화에는 이전 작업자가 “ZARI와 필름 모두 A로 진행합니다”라고 기록하고 필름 수정 중 중단된 과정이 포함되어 있다. 사용자가 그 중단 작업의 계속을 요청했다. 이는 위 방향의 개발 준비를 계속할 근거다. 사용자 대화의 사본을 이번 PR에 기록하며, 별도의 GitHub 승인 댓글·독립 감사 PASS·host 실행 기록을 만들어냈다는 주장은 하지 않는다.

관련 독립 감사: [필름 #19, 80dac7e에 대한 DECISION_REQUIRED](https://github.com/BeautifulMind-JT/film-unit-mv-studio/pull/19#issuecomment-5905575647). 그 감사 결과는 해당 HEAD에 대한 과거 기록이다. 수정한 HEAD는 새로 감사한다.

## 새 모드에 한정한 아키텍처 예외

| 기존 v0.3 계약 | FRAME_ANIMATION_V1에서 채택하는 계약 |
|---|---|
| 컴파일 중 외부 provider 호출 없음 | 고정 FramePlan의 remote compose/encode를 허용한 ExecutionPlan·사용권·예산 안에서 실행한다. creative generation은 별도 승인 경로를 따른다. |
| 빌드 입력은 독립 로컬 복사; 라이브 프로젝트 없이 replay | LOCAL_FULL은 독립 복사·offline replay를 유지한다. DRIVE_BOUNDED는 고정 object/member·revision·hash·archive receipt로 입력을 보존하고 online replay와 명시적 offline restore를 제공한다. 외부 삭제·권한 회수·접근 불가 시 재현 불가를 그대로 보고한다. |
| project mutex로 컴파일 전체 직렬화 | immutable snapshot의 독립 범위를 병렬 계산한다. 프로젝트 상태·선택·receipt·coverage·build ID·seal 쓰기는 단일 coordinator가 직렬화한다. |
| 로컬 전용·로그인/원격 worker 없음 | 앱의 system-browser Google OAuth와 Drive archive, qualification된 CPU/GPU/구독 worker 연결을 새 모드에서 지원한다. |

ARCHITECTURE.md와 PROJECT_SPEC.md에 이 예외를 연결한다. LEGACY_MV·Build 1의 시간축·검수·LOCK·로컬 replay·예산 계약을 유지한다. 기존 프로젝트를 열거나 migration하는 것만으로 모드나 승인을 승계하지 않는다.

## 개발 권한과 별도 결정

설계 #19와 프로그램 등록 #20의 채택·병합 및 승인된 plan commit의 start는 구분한다. 현재 작업은 설계와 18개 실행 노드를 준비하는 단계다. #19→#20 순서로 exact-HEAD 독립 감사와 적용되는 병합 게이트를 거치며, 중앙 program의 실행 소유권·host pin·runtime qualification을 조작하지 않는다.

ANIM-001은 이미 채택한 방향을 ADR·schema 소비자·안전 경계로 확정하는 작업이다. 이 방향을 그대로 구현하는 routine 선택 때문에 같은 결정을 다시 요구하지 않는다. 방향·승인 의미·보존·권한·비용 경계를 벗어나는 변경은 consequential decision 경로를 따른다.

이 결정은 유료 생성 요청, 새 credential 발급, OAuth Cloud project 생성, 외부 GPU/서비스 구매, 실제 작품 검수·최종 출력 승인, 추가 과금, 공개 배포 또는 control-plane runtime/host 활성화를 승인하지 않는다. 연결은 이미 허용된 자원으로 검증하고, 없는 실제 환경은 qualification 미완료로 남긴다.

## ANIM-001에서 유지할 확정 경계

- Google OAuth client는 배포 책임자가 관리하는 등록 앱을 사용한다. installed-app client ID는 공개 설정이며 client secret을 비밀로 신뢰하지 않는다. system browser·PKCE·state·공식 redirect를 검증한다.
- coordinator의 refresh token과 사용자 OAuth token은 worker·LLM·packet·project·build에 전달하지 않는다. 기본 worker 연결은 coordinator가 허용된 immutable 입력을 중개 전송하고 결과를 검증해 Drive에 보관하는 방식이다. worker별 임시 전송 권한은 고정 job·객체·범위·만료에 묶고 취소·회수한다. worker가 직접 Drive에 인증하는 방식은 별도 ADR/권한 승인 전 구현하지 않는다.
- 토큰은 지원 OS credential store에만 보관한다. 사용할 수 없으면 연결을 차단하거나 메모리 한정 세션으로 명시적으로 연결하며, 평문 settings 파일로 자동 전환하지 않는다. 로그아웃·권한 회수·계정 전환 후 접근을 검증한다.
- 취소 요청, 취소 확인, 완료와 취소의 경합, RUNNING/OUTPUT_PENDING_VERIFY의 UNKNOWN을 구분한다. 불명 접수·종료 중에는 예약 해제·대체 제출·새 attempt를 차단한다.
- FAILED_CONFIRMED 후 새 attempt는 사용자가 명시적으로 이어하기를 선택한 때에만 기존 bounded retry allowance와 남은 예약·사용권을 확인해 시작한다. attempt·request identity와 사용량을 표시한다. 자동 재제출을 추가하지 않는다.

이 문서와 설계 문서는 개발 기준이며, 코드 구현·실서비스 qualification·성능 수치·독립 감사·완성 작품 승인 실적으로 집계하지 않는다.
