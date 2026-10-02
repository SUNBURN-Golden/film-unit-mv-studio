# film-unit-mv-studio — 원대한 제품 목표와 상세 실행 계획

2026-10-02 KST · 확대 계획 후보 / 이번 변경은 계획·문서만 작성

## 목표

음원·원문 가사·연출 의도를 검수 가능한 컷·프레임·애니메이션·최종 영상으로 연결하는 제작 작업실. 240초/24fps 기준을 완주하고, 자산/검토/성능/재현을 기반으로 연출 보조·일관성·공식 생성 서비스·다중 출력·시리즈 협업까지 발전시킨다.

사용자 원문: “ZARI, film-unit-mv-studio, Kixprotocol, kixcommerce 전부 최대한 원대하고 .aiops/program.json 넣어줘”, 후속 “원대하고 자세히”. 기능 목록을 넘어 구현 범위·산출물·실패 검증·정확한 선행관계를 작성하라는 지시로 반영했다. 현재 대화는 계획 작성의 근거이며 미래의 모든 상품 정책·실환경 권한·릴리스 결정을 미리 승인한 기록으로 사용하지 않는다.

## 계획을 읽는 방법

- `.aiops/program.json`: schema-v1 형태의 로컬 작업 **35개**. 현재 pointer는 PENDING이므로 실행 입력으로 사용할 수 없다. 기존 23개 정의를 보존하고 12개를 추가했다.
- `docs/aiops/PENDING_NODES.json`: 외부 선행·새 계약·실환경 자격이 필요한 **8개**. 기존 0개를 보존하고 8개를 추가했다. 이 catalogue는 스케줄러가 실행하지 않는다.
- 전체 검토 분모: **43개**. Finance 등 같은 ID의 부분집합을 두 번 세지 않는다. 필요 없는 기능의 연기는 명시적인 범위 개정으로 기록하며 완료로 바꾸지 않는다.
- `docs/aiops/FRAME_ANIMATION_V1_PROGRAM_DRAFT.json`는 위 program과 바이트가 같은 비활성 원본이다. `REGISTRATION_SCOPE_DRAFT.json`은 전체 정의와 문서 해시를 묶는다.
- 기존 작업의 구현을 반복하는 계획이 아니다. 착수 시 현재 소스·증거와 대조해 이미 충족된 요구는 정확한 근거를 연결하고, 남은 gap만 구현한다. 기존 source/의미를 보존한 채 검증 없이 DONE 처리하지 않는다.

## 기준과 기존 등록 PR의 관계

- 관측 main: `7090af381c8f7796e6ea01f8ea64b4546051f163`.
- 기존 시작 PR #22: `8cc3185dcb195b50046ef523e64eed14ebcfb88d` / `aiops/register-program-20261002`. 이번 확대 후보는 그 위의 별도 draft PR이며 원래 시작 PR을 수정하지 않는다.
- 기존 [중앙 #57](https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/57)의 시작 범위·과거 감사는 새 정의에 승계되지 않는다. 원래 시작 PR을 선택할지 확대 범위로 대체할지는 검토 후 한 개의 채택 plan으로 정한다.
- 승인 전 program mirror를 운영 중인 기본 브랜치에 합치지 않는다. 확대 정의를 검토·채택한 뒤 시작 개정에서는 승인된 원본을 복사하고 approval_pointer만 정확한 결정으로 바꾼다. 기존 schema-v1 reader는 PENDING을 실제 거절한다.
- 이번 형식 검증에 사용한 중앙 소스: `a34a38b73f096c9f6597b38a111d95ca12ecd159`. source 읽기/검증 사실은 설치·호스트 자격·독립 감사가 아니다.

## 단계와 의존관계

| 작업 묶음 | 신규 작업 ID |
|---|---|
| 직관적인 제작 작업실 | `film-brief-board`, `film-asset-library`, `film-shot-board` |
| 검토와 수정 | `film-lyrics-review`, `film-review-diff`, `film-quality-diagnostics` |
| 성능과 지속 실행 | `film-resource-forecast`, `film-execution-console`, `film-replay-doctor` |
| 품질과 인계 | `film-workspace-accessibility`, `film-delivery-package`, `film-studio-qualification` |
| 창작·제작 장기 확장 | `film-direction-assistant`, `film-character-continuity`, `film-assisted-editing`, `film-qualified-generation`, `film-multiformat-profile`, `film-team-review`, `film-series-library` |
| 출시와 운영 | `film-expanded-release` |

각 노드의 `depends_on`은 같은 레포의 정확한 ID를 가리킨다. pending의 `depends_on_external`은 producer/consumer의 정확한 repo·program·node를 가리킨다. 목록 순서를 실행 순서로 추정하지 않는다. 독립 branch는 선행이 충족되면 진행할 수 있지만 같은 task/checkout의 writer는 하나다.

외부 의존성이 충족되지 않은 노드를 “문서에 적어두었으니 실행 가능”으로 승격하지 않는다. reader가 채택되거나, 실제 외부 완료와 현재 producer tuple을 검토해 승인된 계획 개정을 만들 때까지 pending을 유지한다. 같은 레포의 미래 계약 노드도 명시된 승격 조건을 만족해야 한다.

## AIOPS가 자율적으로 진행할 범위

- 한 작업 소유자가 구현→테스트→실패 분석→수정→PR을 이어간다. 일상적 알고리즘·리팩터링·레이아웃 선택은 승인 계약 안에서 자율 결정한다.
- 독립 reviewer는 같은 HEAD의 실제 diff와 acceptance를 확인한다. 실패하면 같은 소유자에게 지적을 돌리고 변경 HEAD에서 다시 검토한다. 작성 세션은 자기 독립 감사 PASS를 발급하지 않는다.
- 작업별 실제 산출물과 검증 근거가 필요하다. 빈 모듈·고정 성공 응답·미연결 화면·테스트 대역만으로 실사용 완료를 선언하지 않는다.
- 계정 로그인/OS 권한/제공자 scope·중요 계약/실제 공개·릴리스처럼 사용자 권한이 필요한 결정만 질문한다. 기존 user_merge·A3·RELEASE를 일반 구현 질문으로 대체하지 않는다.
- 계획 노드 개수와 실제 비용·기간은 다르다. 정액 소요 기간·무제한 계정 사용·운영 성능을 약속하지 않는다. 사용량/외부 실행의 미확정 결과는 중복 제출하지 않는다.

## 공통 완료 판정

개발 delivery, 실제 기기/provider qualification, 사용자 화면/작품 수용, 운영 release를 분리한다. 각 근거는 해당 source·contract/profile·environment·artifact에 연결한다. 필요한 실제 환경이 없으면 UNQUALIFIED 또는 PENDING으로 남긴다. 계획 문서의 존재·PR 생성·합성 테스트 성공은 제품 완료가 아니다.

## 신규 작업 상세

### film-brief-board — 기획·음원·가사·레퍼런스의 제작 시작 화면

**배치:** schema-v1 로컬 후보 · **선행:** anim-011 · **검토:** A2/NONE

목표: 처음 작업을 맡겼을 때 필요한 자료와 미확정 사항을 한곳에서 정리한다.

작업 묶음: 직관적인 제작 작업실

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 곡·brief·원문 가사·레퍼런스·원하는 감정을 기존 project 입력에 연결한다
2. 임시 자료와 채택 입력을 구분한다
3. 원문 변경의 영향과 재검수 필요를 설명한다

필수 산출물:
- 프로젝트 준비 화면과 입력 검증
- 누락/잘못된 음원/중복 import 브라우저 시나리오

완료 판정/실패 검증:
1. 가사 길이로 보컬 타이밍을 추정하지 않는다
2. 새 project 생성이 기존 package를 덮어쓰지 않는다
3. 지원 길이·format·임시 slate 상태가 사용자에게 드러난다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-asset-library — 캐릭터·배경·소품·프레임의 자산 탐색

**배치:** schema-v1 로컬 후보 · **선행:** anim-003, anim-008, anim-020 · **검토:** A2/NONE

목표: 어떤 이미지가 어느 컷과 현재 결과에 쓰였는지 찾는다.

작업 묶음: 직관적인 제작 작업실

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 종류/버전/출처/권리 확인/사용 컷으로 자산을 탐색한다
2. 실제 원본·proxy·thumbnail·승인 상태를 구분한다
3. 교체·분리·미사용 자산 정리를 현재 manifest에 연결한다

필수 산출물:
- 자산 library UI와 사용처 read model
- 누락/손상/같은 이름 다른 hash fixture

완료 판정/실패 검증:
1. 파일명만 같다고 같은 자산으로 취급하지 않는다
2. 현재/역사 build가 참조한 자산을 임의 삭제하지 않는다
3. hash 검증이 저작권 허가처럼 표시되지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-shot-board — 콘티·타임라인·프레임·전환의 통합 탐색

**배치:** schema-v1 로컬 후보 · **선행:** anim-005, anim-011, film-asset-library · **검토:** A2/NONE

목표: 컷을 고르면 시간축·프레임·검수·자산 사용처가 함께 움직인다.

작업 묶음: 직관적인 제작 작업실

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. storyboard와 timeline의 stable shot ID 선택을 연동한다
2. 노출·컷 경계·pairwise transition·공백/겹침을 표시한다
3. 변경 draft와 기존 채택 컷을 비교한다

필수 산출물:
- 컷 작업대와 키보드/숫자 편집
- 경계 프레임·전환 소유권 UI fixture

완료 판정/실패 검증:
1. 정수 frame clock과 실제 표시가 일치한다
2. 컷 편집이 가사 cue를 이동시키지 않는다
3. 선택한 컷이 재정렬·reload 후 다른 컷으로 바뀌지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-lyrics-review — 실제 청취와 연결된 가사·한글 자막 검토

**배치:** schema-v1 로컬 후보 · **선행:** anim-006, film-shot-board · **검토:** A2/NONE

목표: 가사 원문을 보존하면서 실제 보컬 타이밍과 읽기 품질을 검토한다.

작업 묶음: 검토와 수정

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 반복 구절·동시 cue·누락·긴 줄·폰트 coverage를 설명한다
2. 청취 위치와 현재 cue 선택을 연결한다
3. 자막 변경에 필요한 부분 검수·LOCK invalidation을 표시한다

필수 산출물:
- 가사/cue 검토 UI와 원문 diff
- 한글/긴 줄/폰트 누락/타이밍 경계 fixture

완료 판정/실패 검증:
1. ASR/import 후보를 사람 검토 완료로 취급하지 않는다
2. clean master와 subbed master의 해시·재검수 범위가 구분된다
3. 폰트 검사 실패가 Final에 들어가지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-review-diff — 검수 지적에서 해당 프레임 수정까지

**배치:** schema-v1 로컬 후보 · **선행:** anim-020, film-shot-board, film-lyrics-review · **검토:** A2/NONE

목표: 결과를 보고 지적한 문제를 정확한 컷·프레임·자산에 되돌린다.

작업 묶음: 검토와 수정

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. finding에 shot/frame range/artifact hash와 문제 유형을 결합한다
2. 수정 전후 비교와 영향 closure를 보여준다
3. 수정 후 해당 지적의 해소와 전체 재생 필요를 구분한다

필수 산출물:
- 검토 finding/수정 대조 화면
- stale approval/다른 cut revision 거절 시나리오

완료 판정/실패 검증:
1. 옛 render에 대한 PASS가 새 render로 승계되지 않는다
2. unrelated 컷 검토를 불필요하게 폐기하지 않는다
3. 작성자 자기 확인과 독립 검토/감독 승인이 구분된다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-quality-diagnostics — 검은 프레임·오디오·시간축·자막 자동 진단

**배치:** schema-v1 로컬 후보 · **선행:** anim-015, anim-020 · **검토:** A2/NONE

목표: 기술적 결함을 자동으로 찾되 미학적 검수와 혼동하지 않는다.

작업 묶음: 검토와 수정

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. decode/PTS/duration/audio channel/clipping/silence/font coverage를 검사한다
2. 의도된 정지·암전·무음과 후보 finding을 구분한다
3. 검출 위치와 재현 명령을 render provenance에 연결한다

필수 산출물:
- QC 진단 모듈과 결과 화면
- 정상/손상/의도된 정지/PTS discontinuity fixtures

완료 판정/실패 검증:
1. 진단이 Final 승인이나 미학 점수로 쓰이지 않는다
2. frame 수와 duration이 240초/24fps 기준에 맞는다
3. 후보 경고를 사용자가 근거와 함께 분류할 수 있다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-resource-forecast — 실행 전 시간·디스크·전송·구독 사용량 예상

**배치:** schema-v1 로컬 후보 · **선행:** anim-017, anim-019 · **검토:** A2/NONE

목표: 작업을 시작하기 전에 필요한 자원과 불확실성을 이해한다.

작업 묶음: 성능과 지속 실행

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 실측 profile에서 예상 CPU/GPU·메모리·cache·전송·출력을 계산한다
2. quote/구독 자격/추가 비용 가능성을 구분한다
3. 자원 부족 시 chunk·quality·route 선택안을 제시한다

필수 산출물:
- 실행 계획 미리보기와 자원 breakdown
- 만료 capability·디스크 부족·quote 변경 fixture

완료 판정/실패 검증:
1. 과거 측정과 현재 장치를 혼동하지 않는다
2. 예상치에 측정 환경과 범위를 표시한다
3. 선택되지 않은 유료 fallback을 자동 실행하지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-execution-console — 실행·취소·업로드·UNKNOWN 상태 작업실

**배치:** schema-v1 로컬 후보 · **선행:** anim-021, film-resource-forecast · **검토:** A2/NONE

목표: 긴 작업의 실제 상태를 보고 안전하게 멈추거나 이어간다.

작업 묶음: 성능과 지속 실행

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. compose/encode/mux/verify/upload/seal을 구별한다
2. 중단 위치·재사용 가능한 artifact·새 attempt 필요를 설명한다
3. network loss·remote running·upload verify를 독립 표시한다

필수 산출물:
- 실행 queue/status UI와 오류별 복귀 흐름
- cancel-complete race·disconnect·partial upload 재현

완료 판정/실패 검증:
1. CANCEL_REQUESTED를 종료 확정으로 표시하지 않는다
2. UNKNOWN에 새 원격 작업을 중복 제출하지 않는다
3. 업로드 완료와 readback/hash 검증 완료가 구분된다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-replay-doctor — 재현·누락 자산·이전 버전 진단

**배치:** schema-v1 로컬 후보 · **선행:** anim-020, anim-021, film-asset-library · **검토:** A2/NONE

목표: 과거 build를 재생하거나 재구축할 수 없는 원인을 정확히 찾는다.

작업 묶음: 성능과 지속 실행

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. source/recipe/clean/subbed/encode manifest 연결을 검사한다
2. 자산 누락·hash 차이·도구 버전 차이·unsupported profile을 분류한다
3. 가능한 동일 재현과 새 결과 생성 분기를 구분한다

필수 산출물:
- build doctor와 재현 packet
- corrupt pack/잘못된 index/range/schema fixture

완료 판정/실패 검증:
1. 재구축으로 달라진 hash를 기존 build라고 부르지 않는다
2. bounded restore가 전체 원본 다운로드로 몰래 바뀌지 않는다
3. credential/토큰이 재현 packet에 포함되지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-workspace-accessibility — 제작 작업실의 키보드·작은 화면·오류 UX

**배치:** schema-v1 로컬 후보 · **선행:** film-brief-board, film-review-diff, film-execution-console · **검토:** A2/NONE

목표: 실무 제작 흐름을 전문 용어를 몰라도 따라갈 수 있게 한다.

작업 묶음: 품질과 인계

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 준비→동작→검토→수정→출력 단계의 다음 행동을 명확히 한다
2. 긴 타임라인의 keyboard focus·숫자 이동·확대·reduced motion을 제공한다
3. 빈 project·missing credential·오류·재시작 상태를 다듬는다

필수 산출물:
- 일관된 제작 화면과 문맥 도움말
- 전체 keyboard journey/desktop 반응형 evidence

완료 판정/실패 검증:
1. 사용자가 raw JSON을 편집해야만 핵심 작업을 끝내지 않게 한다
2. 색상만으로 검수·실패·진행 상태를 구분하지 않는다
3. 실패 후 입력과 선택한 frame이 보존된다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-delivery-package — 작품 출력·소스·검수·재현 자료 인계

**배치:** schema-v1 로컬 후보 · **선행:** film-quality-diagnostics, film-replay-doctor, film-workspace-accessibility · **검토:** A2/NONE

목표: 영상 파일 하나와 함께 그 파일이 무엇인지 증명할 자료를 전달한다.

작업 묶음: 품질과 인계

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. clean/subbed master·thumbnail·자막·manifest·QC·검수 범위를 묶는다
2. 전달 profile·원본 보존·다운로드 무결성을 확인한다
3. Preview/Final 후보/감독 채택/외부 공개 상태를 표시한다

필수 산출물:
- 재현 가능한 delivery bundle
- 첫 설치부터 bundle까지 사용 안내

완료 판정/실패 검증:
1. 파일명 Final만으로 승인 상태가 바뀌지 않는다
2. 포함 artifact의 hash와 current 승인 scope가 일치한다
3. 비밀정보와 원치 않는 개인 자료가 bundle에 들어가지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-studio-qualification — 4분 제작 작업실 전체 qualification

**배치:** schema-v1 로컬 후보 · **선행:** anim-023, film-delivery-package · **검토:** A2/NONE

목표: 240초 이미지 애니메이션을 준비부터 검수 인계까지 실제로 완주한다.

작업 묶음: 품질과 인계

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 어려운 W00 컷을 먼저 포함하고 경로 유지/변경 판단을 기록한다
2. 5760 프레임·ones/twos·transitions·한글 cue·Drive/worker route를 검증한다
3. source/실환경/작품 수용/릴리스의 네 분모를 유지한다

필수 산출물:
- 240초 시나리오와 경로별 실제 증거
- 현재 소스의 제품 인계/미완료 표

완료 판정/실패 검증:
1. fake provider 성공을 구독/원격 실행 qualification으로 세지 않는다
2. 실제 정상속도 전체 재생 검토가 필요한 범위를 명시한다
3. 선택된 route의 시작/취소/재개/완료와 replay가 모두 확인된다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

### film-direction-assistant — 기획·연출·컷 설계 보조 에이전트

**배치:** pending catalogue · **선행:** film-brief-board, anim-007 · **검토:** A3/ARCHITECTURE

목표: 곡과 감독 의도에서 검토 가능한 연출안·컷안을 제안한다.

작업 묶음: 창작·제작 장기 확장

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 구조화 brief·금지사항·원문 가사·타이밍 근거를 입력으로 묶는다
2. 다른 연출안의 차이와 각 컷의 역할을 설명한다
3. 감독 채택 전 project 정본과 LOCK을 변경하지 않는다

필수 산출물:
- 연출 제안 adapter/packet/비교 UI
- 원문 변조·근거 없는 타이밍·예산 초과 거절 fixture

완료 판정/실패 검증:
1. LLM 제안이 감독 승인으로 기록되지 않는다
2. 근거 없는 가사 재작성·저작권 허가 주장이 없다
3. 사용자가 변경한 의도를 다음 제안이 보존한다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

### film-character-continuity — 인물·배경·소품의 장면 간 일관성 관리

**배치:** pending catalogue · **선행:** film-direction-assistant, film-asset-library · **검토:** A3/ARCHITECTURE

목표: 여러 컷에 걸친 캐릭터와 미술 방향을 검토 가능한 기준으로 유지한다.

작업 묶음: 창작·제작 장기 확장

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. approved reference sheet와 장면별 허용 변화를 버전 관리한다
2. 얼굴/의상/비율/색/소품의 비교 후보를 만든다
3. 오류 finding을 정확한 자산·컷 수정으로 연결한다

필수 산출물:
- 연속성 기준 sheet·비교 도구
- 동일/변형/불일치 shot fixture와 검토 UI

완료 판정/실패 검증:
1. 자동 유사도 점수가 채택 판정을 대체하지 않는다
2. 스타일 기준 변경의 영향 컷이 모두 나타난다
3. 소수 레퍼런스의 성공을 전체 작품 일관성으로 일반화하지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

### film-assisted-editing — 음악·서사 기반 컷 대안과 비파괴 편집

**배치:** pending catalogue · **선행:** film-direction-assistant, film-shot-board · **검토:** A3/ARCHITECTURE

목표: 리듬과 이야기 흐름을 바꾸는 편집안을 원본 보존 상태로 비교한다.

작업 묶음: 창작·제작 장기 확장

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. source timeline과 대안 timeline을 분리한다
2. 음악 분석 관측·서사 의도·컷 추천 근거를 표시한다
3. cue·frame ownership·transition 재검증 뒤 채택한다

필수 산출물:
- 비파괴 편집 대안과 비교 재생
- 타임라인 migration/undo/invalid cue fixture

완료 판정/실패 검증:
1. beat 검출을 보컬 cue 정본으로 쓰지 않는다
2. 채택 전 원본 timeline을 덮어쓰지 않는다
3. 총 frame coverage와 모든 최종 artifact provenance가 새 revision에 묶인다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

### film-qualified-generation — 실제 구독·공식 생성 경로별 연결 자격

**배치:** pending catalogue · **선행:** anim-016, anim-019, film-character-continuity · **검토:** A3/ARCHITECTURE

목표: 사용 가능한 이미지/구간 생성 서비스를 실제 능력과 권한에 맞춰 연결한다.

작업 묶음: 창작·제작 장기 확장

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. provider별 모델·입출력·기간·가격·entitlement·재사용 권리를 확인한다
2. official API/수동 packet/정상 지원 UI 경로를 구분한다
3. 지급/응답 유실·부분 생성·사용량 소진을 대사한다

필수 산출물:
- 실제 provider qualification packet
- 비용 quote·사용량 journal·결과 import evidence

완료 판정/실패 검증:
1. 가입 플랜 이름만으로 기능·무료·상업 권리를 추정하지 않는다
2. UNKNOWN 재전송과 묵시적 유료 경로 전환을 차단한다
3. 실제 계정·작업 범위에서 확인한 결과만 QUALIFIED로 기록한다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

### film-multiformat-profile — 4K·세로형·고프레임 출력 프로파일

**배치:** pending catalogue · **선행:** film-delivery-package, anim-015, film-assisted-editing · **검토:** A3/ARCHITECTURE

목표: 기준 작품을 보존하면서 매체별 출력 요구를 별도 프로파일로 지원한다.

작업 묶음: 창작·제작 장기 확장

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 해상도/aspect/FPS/color/audio/subtitle safe area를 versioned profile로 정의한다
2. crop/reframe/retime과 단순 encode 차이를 구분한다
3. profile별 필요한 자원·기기·검수 범위를 측정한다

필수 산출물:
- 출력 profile ADR·encoder 적합성 fixture
- 세로/4K 등 실제 표본 및 QC 보고서

완료 판정/실패 검증:
1. 24fps 프레임을 고프레임으로 반복한 것을 새 동작 정보로 주장하지 않는다
2. crop이 자막·얼굴·필수 소품을 자르면 검수 대상이 된다
3. 기존 240초 기준 자격이 새 profile에 자동 승계되지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

### film-team-review — 감독·편집자·작화 담당 협업과 접근권

**배치:** pending catalogue · **선행:** film-review-diff, film-delivery-package · **검토:** A3/ARCHITECTURE

목표: 작업 분담과 검토를 지원하되 최종 소스와 승인 주체를 명확히 한다.

작업 묶음: 창작·제작 장기 확장

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 역할별 수정/제안/검수/출력 권한과 optimistic conflict를 정의한다
2. asset/shot branch와 승인된 merge를 설계한다
3. 외부 공유 만료·삭제·파일 접근·개인정보 흐름을 정한다

필수 산출물:
- 협업 ADR·권한/충돌 UI
- stale approval/동시 수정/접근 철회 tests

완료 판정/실패 검증:
1. 여러 편집자가 같은 정본을 무단 덮어쓰지 않는다
2. 댓글·파일 다운로드가 작품 승인으로 변환되지 않는다
3. credential을 worker·리뷰어에게 무차별 배포하지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

### film-series-library — 여러 작품·시리즈의 재사용과 예산 관리

**배치:** pending catalogue · **선행:** film-team-review, film-character-continuity, film-resource-forecast · **검토:** A3/ARCHITECTURE

목표: 시리즈의 세계관·자산·실측 자원 정보를 안전하게 재사용한다.

작업 묶음: 창작·제작 장기 확장

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. 작품별 권리·스타일 revision·등장인물과 재사용 허가를 연결한다
2. 공통 자산 변경이 과거 build를 덮어쓰지 않게 한다
3. 프로젝트별 quote·확정 사용량·예산을 분리한다

필수 산출물:
- series workspace와 asset reuse manifest
- 작품 간 오염/권리 만료/예산 attribution fixture

완료 판정/실패 검증:
1. A 작품 승인과 라이선스가 B 작품으로 자동 승계되지 않는다
2. 중복 저장·전송 절감은 실제 측정과 함께 보고한다
3. 실제 작업이 없는 예정 사용량은 청구액으로 표시되지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

### film-expanded-release — 확장 제작 플랫폼 릴리스 검수

**배치:** pending catalogue · **선행:** film-studio-qualification, film-qualified-generation, film-multiformat-profile, film-series-library · **검토:** A3/RELEASE

목표: 실제 작품과 설치 가능한 도구를 구분해 확장 출시 범위를 검수한다.

작업 묶음: 출시와 운영

설계 근거: PROJECT_SPEC.md; ARCHITECTURE.md; docs/FRAME_ANIMATION_V1_DESIGN_KO.md; docs/FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md; docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md

구현 범위:
1. provider/장치/profile/작품별 qualification을 집계한다
2. 재현/비용/접근성/권리/데이터 인계의 미해결 항목을 닫는다
3. 운영 지원과 업데이트·호환 정책을 전달한다

필수 산출물:
- 현재 소스의 release candidate 및 범위별 검수 자료
- 사용자 최종 수용/연기 항목

완료 판정/실패 검증:
1. 시험 작품 하나의 성공을 모든 모델·기기로 확대하지 않는다
2. 생산물 공개와 도구 출시 권한을 구분한다
3. 실제 승인 전 공개 배포·유료 실행을 시작하지 않는다

공통 경계: 원곡·가사 원문·검토 cue·기존 LOCK을 보존한다. 신규 결과에 기존 검수/LOCK을 승계하지 않는다. 240초/24fps 기준과 legacy 모드를 유지한다. Preview/fake/source delivery는 실제 작품 Final·서비스 qualification이 아니다. 유료 생성·계정 동의·작품 승인·외부 배포는 기존 범위별 권한을 따른다.

증거/인계: 실제 변경 파일·실행 명령·성공 및 실패 사례·정확한 소스 SHA를 PR에 남긴다. 기존 충분한 coverage는 재사용하고 새 gap만 검증한다. UI 변경은 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드 동작을 확인한다. 실환경 부재는 미검증으로 남긴다. 독립 검토 지적과 CI 실패는 같은 소유자가 수정하고 변경 HEAD를 다시 검토한다. 일반 구현 선택은 자율 결정하며 계정·중요 계약·실사용 승인만 구체적인 결정을 요청한다.

승격 조건: 상세 계약 채택·정확한 소스의 독립 검토·범위별 실제 환경 자격 확인 뒤 계획 개정으로 승격한다. 이 항목은 pending catalogue이며 현재 schema-v1 dispatch 대상이 아니다. depends_on_external을 spec 문장으로 바꾸거나 삭제하여 우회하지 않는다.

## 전체 작업 색인

| ID | 작업 | 배치 | 선행 |
|---|---|---|---|
| anim-001 | ANIM-001: FRAME_ANIMATION_V1 계약 ADR/명세 (frame·자산·승인·storage·execution·encoder) | local candidate | 없음 |
| anim-002 | ANIM-002: 명시적 migration·backup·legacy reader | local candidate | anim-001 |
| anim-003 | ANIM-003: 프레임 자산 import·version·hash·resolver와 draft Preview | local candidate | anim-001, anim-002 |
| anim-004 | ANIM-004: frame clock·노출(ones/twos)·시퀀스 정규화 | local candidate | anim-003 |
| anim-005 | ANIM-005: pairwise 전환·frame_map·길이 검산 | local candidate | anim-004 |
| anim-006 | ANIM-006: 컷/전환 검수·Final 후보·inventory·Build 2 replay | local candidate | anim-005 |
| anim-007 | ANIM-007: PLAN/WAVE/FINAL LOCK·W00·경로 결정 (첫 기능 묶음 완료) | local candidate | anim-006 |
| anim-008 | ANIM-008: native C 계층·pivot·변형·대체 그림·mask | local candidate | anim-007 |
| anim-009 | ANIM-009: A 경로 제어 이미지·세부 packet·browser hand-off | local candidate | anim-007 |
| anim-010 | ANIM-010: 생성 adapter (capability·구간·quote·ledger·UNKNOWN·PTS), fake provider | local candidate | anim-008, anim-009 |
| anim-011 | ANIM-011: 사용자 흐름 (준비→동작→검토→수정→출력) UI 연결 | local candidate | anim-007, anim-008, anim-009 |
| anim-012 | ANIM-012: 240초 통합 회귀·desktop packaging·문서 | local candidate | anim-010, anim-011 |
| anim-013 | ANIM-013: 브라우저 Google 로그인·Drive archive·bounded cache·restore | local candidate | anim-001, anim-003, anim-006 |
| anim-014 | ANIM-014: 실행 계약 (FrameStream·ExecutionPlan·worker·receipt·UNKNOWN) | local candidate | anim-001, anim-004, anim-007, anim-013 |
| anim-015 | ANIM-015: encode/mux/verify 분리와 복수 인코더 (FFmpeg·NVIDIA native·VideoToolbox·GStreamer·service) | local candidate | anim-001, anim-004, anim-006 |
| anim-016 | ANIM-016: AI 구독 실행 (entitlement·probe·packet/notebook·import) | local candidate | anim-009, anim-014, anim-015 |
| anim-017 | ANIM-017: 성능 scheduler (부분 재컴파일·병렬 합성·prefetch·캐시 재사용) | local candidate | anim-005, anim-013, anim-014, anim-015, anim-016 |
| anim-018 | ANIM-018: 원격 통합 (240초·1080p·Drive→worker→archive·desktop UI) | local candidate | anim-008, anim-012, anim-013, anim-014, anim-015, anim-016, anim-017 |
| anim-019 | ANIM-019: scope별 실행 자격 registry·갱신·성능 측정 binding | local candidate | anim-001, anim-013, anim-014, anim-015, anim-016 |
| anim-020 | ANIM-020: canonical render provenance·invalidate closure·부분 재현 | local candidate | anim-003, anim-004, anim-005, anim-006, anim-017, anim-019 |
| anim-021 | ANIM-021: durable worker journal·upload 재개·archive commit/seal | local candidate | anim-013, anim-014, anim-015, anim-019 |
| anim-022 | ANIM-022: W00 경로 판단·실제 전체 재생·current 작품 승인 | local candidate | anim-007, anim-010, anim-011, anim-012, anim-019, anim-020, anim-021 |
| anim-023 | ANIM-023: 23-node 전체 closeout·현재 자격/작품/릴리스 근거 | local candidate | anim-001, anim-002, anim-003, anim-004, anim-005, anim-006, anim-007, anim-008, anim-009, anim-010, anim-011, anim-012, anim-013, anim-014, anim-015, anim-016, anim-017, anim-018, anim-019, anim-020, anim-021, anim-022 |
| film-brief-board | 기획·음원·가사·레퍼런스의 제작 시작 화면 | local candidate | anim-011 |
| film-asset-library | 캐릭터·배경·소품·프레임의 자산 탐색 | local candidate | anim-003, anim-008, anim-020 |
| film-shot-board | 콘티·타임라인·프레임·전환의 통합 탐색 | local candidate | anim-005, anim-011, film-asset-library |
| film-lyrics-review | 실제 청취와 연결된 가사·한글 자막 검토 | local candidate | anim-006, film-shot-board |
| film-review-diff | 검수 지적에서 해당 프레임 수정까지 | local candidate | anim-020, film-shot-board, film-lyrics-review |
| film-quality-diagnostics | 검은 프레임·오디오·시간축·자막 자동 진단 | local candidate | anim-015, anim-020 |
| film-resource-forecast | 실행 전 시간·디스크·전송·구독 사용량 예상 | local candidate | anim-017, anim-019 |
| film-execution-console | 실행·취소·업로드·UNKNOWN 상태 작업실 | local candidate | anim-021, film-resource-forecast |
| film-replay-doctor | 재현·누락 자산·이전 버전 진단 | local candidate | anim-020, anim-021, film-asset-library |
| film-workspace-accessibility | 제작 작업실의 키보드·작은 화면·오류 UX | local candidate | film-brief-board, film-review-diff, film-execution-console |
| film-delivery-package | 작품 출력·소스·검수·재현 자료 인계 | local candidate | film-quality-diagnostics, film-replay-doctor, film-workspace-accessibility |
| film-studio-qualification | 4분 제작 작업실 전체 qualification | local candidate | anim-023, film-delivery-package |
| film-direction-assistant | 기획·연출·컷 설계 보조 에이전트 | pending | film-brief-board, anim-007 |
| film-character-continuity | 인물·배경·소품의 장면 간 일관성 관리 | pending | film-direction-assistant, film-asset-library |
| film-assisted-editing | 음악·서사 기반 컷 대안과 비파괴 편집 | pending | film-direction-assistant, film-shot-board |
| film-qualified-generation | 실제 구독·공식 생성 경로별 연결 자격 | pending | anim-016, anim-019, film-character-continuity |
| film-multiformat-profile | 4K·세로형·고프레임 출력 프로파일 | pending | film-delivery-package, anim-015, film-assisted-editing |
| film-team-review | 감독·편집자·작화 담당 협업과 접근권 | pending | film-review-diff, film-delivery-package |
| film-series-library | 여러 작품·시리즈의 재사용과 예산 관리 | pending | film-team-review, film-character-continuity, film-resource-forecast |
| film-expanded-release | 확장 제작 플랫폼 릴리스 검수 | pending | film-studio-qualification, film-qualified-generation, film-multiformat-profile, film-series-library |

## 계획 검증 명령

이 검사는 계획의 구조와 해시를 확인한다. 제품 구현 테스트·독립 A3·실제 실행 자격을 대신하지 않는다.

```bash
python3 scripts/validate_program_expansion.py
# 네 레포의 이번 확대 후보를 같은 상위 디렉터리에 checkout한 경우
python3 scripts/validate_program_expansion.py --workspace /path/to/sibling-repositories
```

JSON 중복 key·ID 충돌·잘못된 선행·순환·전체 정의 해시·정본 mirror·문서 해시·Finance alias를 검사한다. 전체 workspace 검사는 외부 선행의 실제 ID와 네 레포 결합 DAG까지 확인한다. 작업 완료 사실이나 외부 승인 여부를 자동 추정하지 않는다.

