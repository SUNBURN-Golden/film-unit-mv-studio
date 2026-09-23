# 이벤트 접수·review 배정·감사 전달·준비 판정

적용 task: https://github.com/BeautifulMind-JT/kix-protocol/issues/40 / revision 1. 대상: `FILM_UNIT` / `BeautifulMind-JT/film-unit-mv-studio`.
공통 source: [KIX PR #39](https://github.com/BeautifulMind-JT/kix-protocol/pull/39), `f179be8fc3c0c590b3194c26663aecdfe7a4a679`.
원본 구현 이력 CP-FLOW-002는 KIX의 이력이며 이 저장소의 task 번호가 아니다.
대상 exact HEAD의 독립 감사·host 설치·User 활성화는 별도 gate다. 기존 project 계약을 대체하지 않는다.

## 구현된 경계

- Slack `/astra dispatch|refresh|status PROJECT ISSUE`의 원문 서명·5분 timestamp·workspace/app/user/channel allowlist 검증.
- GitHub webhook은 서명 검증 후 refresh 신호로만 처리. payload의 PASS·HEAD·actor를 검증 결과로 사용하지 않는다.
- SQLite inbox/outbox가 이벤트/요청을 직렬화한다. NOT_STARTED를 포함해 기존 요청을 재사용한다. 외부 send 전 SUBMITTING을 durable commit한다.
- GitHub canonical issue에 action을 먼저 투영하고 receipt를 확인한 뒤 외부 전달한다. projection 요청 ID와 실제 review/audit 요청 ID는 구분한다.
- 고정 승인 route로 non-author reviewer를 선정하고 GitHub의 requested_reviewers API로 배정한다. 임의의 저가 모델 fallback은 없다.
- 현재 PR·CI·독립 review를 API에서 새로 읽고 gate를 계산한다. reviewer 발견 A3는 architecture audit을 추가한다. 기존 RELEASE/MILESTONE 의무를 지우지 않는다.
- 필요한 audit/decision과 상태만 Slack의 지정 채널로 전달한다. 문장을 생성하는 LLM 호출, polling, 자동 merge는 없다.
- Grok Build/GLM의 host preflight 호출 경로와 정확한 runtime/binary/wrapper/harness/report에 결합된 lane qualification 검사.

**아직 실제 운영 활성화가 아니다.** 이 PR은 Slack 앱 설치·HTTPS 배포·signing secret 설정,
production runner 연결·trusted workflow boundary·새 provider 로그인/과금/격리 검증을 수행하지 않는다.
review 배정은 GitHub reviewer request다. 해당 계정의 CLI reviewer를 자동 시작하는 host adapter는
별도 검증 대상이며, native GitHub request만으로 모델 세션 시작을 주장하지 않는다.
기존 runtime v1의 builder launch만 재사용한다. READY_FOR_MERGE는 merge 권한이나 명령이 아니다.

## 소스와 테스트

- `scripts/control_plane_flow.py`: 서명·identity·판정·durable inbox/outbox·coordinator.
- `scripts/control_plane_flow_gateway.py`: 실제 GitHub/Slack HTTPS ports, WSGI ingress.
- `scripts/control_plane_flow_cli.py`: 명시적 host preflight / offline qualification.
- `scripts/test_control_plane_flow.py`: 위조·stale·A3·revocation·동시성·응답 유실·collector 회귀.

```sh
python3 -m py_compile scripts/control_plane_flow*.py
python3 -m unittest discover -s scripts -p 'test_control_plane_flow.py' -v
```

기존 Control Plane Runtime CI의 `test_control_plane*.py` discovery에도 포함된다.
로컬 테스트는 mock HTTP와 임시 SQLite를 사용한다. 실제 provider·Slack·host 검증과 다르다.
제품 PR 코드를 제어용 self-hosted runner에서 실행하지 않는다.

## 운영 설치 계약

배포는 별도 exact-HEAD 독립 감사와 User 승인을 요구한다. root 소유·builder/runner 쓰기 금지
설치 경로에서 `python3 -I`로 로드한다. gateway와 core의 source SHA-256을 보호된 정책에 고정한다.
`create_app('/etc/astra/flow-policy.json')`은 WSGI 객체를 반환할 뿐 server를 열지 않는다.
TLS reverse proxy, body/time/rate 제한, 검증된 WSGI server 설치는 operator 배포 영역이다.
서버가 연결을 받는 것과 polling/상주 LLM reasoning은 다르다.

`flow-policy.example.json`은 모든 기능 off·빈 actor/channel registration으로 배포 불가능한 예시다.
flow 계정은 기존 builder/runner/control 계정과 격리하고 provider credential은 주지 않는다.
GitHub token은 필요한 repo read, issue projection, reviewer request, 승인된 workflow dispatch만 허용한다.
GitHub 권한이 branch별 merge 금지를 자동 제공한다고 가정하지 않는다. 실제 ruleset/권한 경계 검증이 필요하다.

flow 전용 DB의 부모 디렉터리는 계정 소유 0700, DB는 0600이어야 한다.
DB는 operator가 `Store(path, initialize=True)`로 최초 한 번 만들며 실행 중 누락 DB를 재생성하지 않는다.
**이 DB는 이벤트 전달용이다. 기존 host admission DB·활성 session 상태를 대체하거나 초기화하지 않는다. host 일일 신규 launch 상한은 별도 runtime policy에서 unlimited다.**
서로 다른 host가 SQLite를 공유한다고 distributed lock을 주장하지 않는다.

서비스 계정 secret injection 이름:
`ASTRA_FLOW_GITHUB_TOKEN`, `ASTRA_FLOW_SLACK_TOKEN`,
`ASTRA_FLOW_SLACK_SIGNING_SECRET`, `ASTRA_FLOW_GITHUB_WEBHOOK_SECRET`.
값은 GitHub/Slack transcript, source, task packet, builder 환경에 넣지 않는다.

외부 endpoint는 `/slack/commands`, `/github/events`다. Slack 앱의 실제 workspace/app/user/channel ID를
확인한 뒤 allowlist에 넣는다. 현재 채널 ID나 앱이 없으면 기능을 켜지 않는다.
raw body의 response_url/token은 저장하지 않는다. ACK는 접수만 의미한다.
16개 처리 슬롯이 차면 durable NOT_STARTED를 보존하고 503을 반환한다. 동일 이벤트 재전달은
새 request를 만들지 않고 NOT_STARTED만 깨운다. PROCESSING/SUBMITTING/UNKNOWN은 자동 재실행하지 않는다.
미완료 이벤트는 operator가 기존 요청/외부 receipt를 대조해야 한다. 타이머 재시도·DB 초기화 금지.

## Canonical task와 신뢰 근거

등록 key: `owner/repo#issue`. 한 `(repo, task_id)`에는 issue 하나만 등록한다.
registration은 task_id, User GitHub actor, comment_id, comment body SHA-256,
canonical issue body SHA-256을 포함한다. issue body나 pinned comment가 바뀌면 즉시 차단한다.

User task comment의 형식: `<!-- ASTRA_FLOW_TASK_V1 -->` + newline + 단일 JSON object.
JSON은 task/revision/repo/base, task_pointer, approval_pointer(해당 comment URL), builder identity,
author identities/sessions, PR 번호, audit_floor, astra_gate, required_checks, dependencies, blockers를 담는다.
comment URL은 먼저 생성한 comment의 URL을 사용하고 최종 body를 고정한다. SHA를 본문 자체에 넣지 않는다.
required_checks는 비어 있을 수 없으며 name, app_id, workflow_id, path, workflow_blob, events를 고정한다.
workflow source blob과 현재 head의 최신 run/attempt가 맞아야 하며 skipped/neutral은 success가 아니다.
CI pagination이 한도를 넘으면 일부 목록으로 PASS하지 않는다.

`dependencies`: `{repository, pr, merge_sha}` 목록. 각 merge를 API에서 다시 확인한다.
저장소별 자동 해석이 불가능한 추가 gate는 별도의 current-head User attestation으로 받는다.
registration.prerequisites는 actor/comment_id/sha256을 고정하며 comment marker는
`<!-- ASTRA_FLOW_PREREQUISITES_V1 -->`다. body는 active:true, result:PASS, subject를 가진다.
이 subject는 repo/task/revision/head/base/task_digest이며 **policy_revision은 제외**한다.
외부 attestation의 hash를 포함하는 policy hash를 다시 attestation 안에 넣는 순환을 피한다.
이 경로는 추가 gate를 자동으로 증명했다고 주장하지 않는다.

`review_routes`: builder ID → 별도 GitHub reviewer identity.
`review_lanes`: identity → enabled/read_only_verified/approval_pointer 및 User comment binding.
review lane comment marker는 `<!-- ASTRA_FLOW_LANE_V1 -->`; active/identity/read_only_verified를 고정한다.
별도 GitHub account도 독립성을 자동 증명하지 않는다. 작성 미참여와 read-only host/credential evidence를
검증한 뒤에만 User가 lane을 등록한다. 현재 단일 공유 GitHub identity는 author conflict로 차단된다.

`auditor_binding`: User comment actor/id/hash. marker `<!-- ASTRA_FLOW_AUDITOR_V1 -->`에
active, identity, task_id, revision, read_only_verified와 scope를 기록한다.
철회/교체는 **그 정본 comment 수정/삭제 또는 보호된 등록 변경**으로 수행한다.
다른 Slack 문장이나 새로운 비정본 comment는 revocation event로 추측하지 않는다.
fresh GET/hash가 달라지면 같은 HEAD의 과거 PASS도 수락하지 않는다.

## 결과와 정확한 요청 identity

review/audit 결과는 해당 HEAD의 native GitHub review로 제출한다.
body marker: `<!-- ASTRA_FLOW_RESULT_V1 -->` + newline + JSON.
GitHub action projection에서 받은 subject/request_id/attempt_id/identity/designation을 그대로 사용한다.
phase는 review/audit, result는 PASS/PASS_WITH_NOTES/FAIL/DECISION_REQUIRED,
required_depth/verified_depth는 A1~A3, contract_change는 boolean, blockers는 배열이다.
PASS 계열은 GitHub state APPROVED도 요구한다. actor는 API의 실제 user.login이며 JSON 주장이 아니다.
GitHub review ID를 인증된 제출 session에 결합한다. 이 ID는 provider 실행 session ID가 아니다.
READ_ONLY는 검증된 lane을 통해서만 수락한다. 새 HEAD/base/revision/policy나 dismissal은 다시 검사한다.
audit 결과는 gate와 scope_digest도 exact match해야 한다. RELEASE/MILESTONE scope는 명시해야 한다.

GithubPorts의 추가 attestation은 아직 사람/승인된 collector가 제공하는 경계다.
모든 repo의 기술·법무·운영 gate를 코드가 자동 판정한다고 주장하지 않는다.
기록의 source-of-truth와 외부 send 사이 완전한 분산 transaction은 없다. send 직전 재조회와
outbox CAS를 쓰되, 외부 상태가 그 직후 바뀔 수 있으므로 실행/최종 gate에서도 새로 검증한다.
transport CONFIRMED는 요청/메시지 전달만 의미한다. 의미론적 PASS와 구분한다.

## Grok Build / GLM staging 검증

```sh
python3 -I scripts/control_plane_flow_cli.py host-preflight --builder GROK_BUILD
python3 -I scripts/control_plane_flow_cli.py host-preflight --builder GLM
python3 -I scripts/control_plane_flow_cli.py qualify-lane \
  --report report.json --approval approval.json --runtime-sha <audited-implementation-sha>
```

host-preflight는 기존 sudo-authenticated protected helper만 호출한다. disabled lane을 우회하지 않는다.
실제 staging operator가 새 lane의 설치·binary/wrapper hash·정확한 harness/model/version·전용 인증·
UID 격리·지속 session·중복/UNKNOWN·workflow boundary·quota를 검증하고 evidence URL을 남겨야 한다.
report의 checks는 cli/authentication/credential_isolation/durable_session/duplicate_unknown/
trusted_workflow_boundary/quota_policy이며 모두 PASS가 아니면 qualification이 실패한다.
User approval은 report digest에 결합한다. qualification 성공도 production_enabled:false다.
새 구현 SHA 또는 wrapper/harness 변경은 다시 검증한다. provider/account quota 문제를 이유로 ledger를 지우지 않는다.

Grok Build의 공식 headless surface는 `grok -p`, named session, JSON output, no-auto-update다.
CLI의 session 파일 존재는 process 생존 증명이 아니다. 기존과 같은 protected supervisor 증거가 필요하다.
GLM은 모델이고 ZCode는 harness다. `zcode -p`를 존재한다고 가정하지 않는다.
OpenCode+GLM은 공식 문서상 별도 후보이며 자동 대체/과금 변경을 하지 않는다.
설치된 harness와 해당 구독 사용 경로를 User가 확정하기 전에는 GLM lane을 켜지 않는다.

Primary references (implementation-time check):
- https://docs.slack.dev/authentication/verifying-requests-from-slack/
- https://docs.x.ai/build/cli/headless-scripting
- https://docs.z.ai/devpack/tool/opencode

## 완료 판정과 제외

이 PR의 테스트 PASS는 author verification이다. 새 exact-HEAD independent review,
실제 Slack 왕복, provider preflight, trusted workflow boundary와 host 자격증명 검증이 남는다.
기존 #32의 PASS를 이 추가 코드에 승계하지 않는다. 다른 4개 repo에는 검토 전 복제하지 않는다.
5개 repo 자동 배포, live activation, CLI reviewer 자동 실행, 자동 merge는 이 PR의 완료 주장이 아니다.
