# 전체 등록 범위와 별도 시작 PR — 초안

2026-10-01 대표님 결정 1=A에 따라 승인 전 계획과 범위 manifest는 `docs/aiops/`의 NON_EXECUTABLE_DRAFT에만 둔다. `docs/aiops/FRAME_ANIMATION_V1_PROGRAM_DRAFT.json`의 `approval_pointer`는 `PENDING_APPROVAL_DO_NOT_DISPATCH` 하나다. `.aiops/program.json` 및 `.aiops/registration-scope.json`은 이 PR에 없다. 이 초안 PR의 병합만으로 실행/시작 승인이나 새 감사 PASS가 생기지 않는다.

## 범위와 변경 이력

#19 c66a90cedb4ef0da5719b69c9771da0b46fd7868 revision3 설계, ANIM-001–018 storage/execution/encoder 구현 후보, revision4 전송·pack·완료 facets, ANIM-019–023 capability/manifest/journal/작품 검토 후속을 전체23 정의로 묶었다. 이전18 정의 승인/실행 분모를 새23 정의 승인으로 전이하지 않는다.

긴 approval_pointer에 섞여 있던 범위·audit·runtime 설명을 이 문서로 옮겼다. 자기 승인 문구와 #46 채택 요구는 삭제했다. 등록 범위는 plan 및 모든 pending catalogue의 ID·spec·선행·외부 선행·audit/게이트·User-only/위임 flag·전체 완료 분모를 함께 검토한다. Finance는 KIX catalogue와 동일한 ID/정의의 부분집합이며 분모에 다시 더하지 않는다. 부분 승인/거절/연기는 새 적용 범위 개정과 영향 정의/이전 분모를 기록한다. 제외된 정의를 자동 DONE 처리하지 않는다.

## 시작 PR 절차

1. 대표님이 실제 시작을 결정하면 승인된 immutable source commit·전체 manifest digest·plan/catalogue 정의 digest·정확한 독립 감사와 범위/시작 결정을 하나의 durable 대표님 결정 기록에 묶는다. 2026-10-01 정책 C 댓글은 병합 정책 결정이며 제품별 시작 승인을 대신하지 않는다.
2. 해당 승인 commit의 `docs/aiops/FRAME_ANIMATION_V1_PROGRAM_DRAFT.json` 바이트를 그대로 `.aiops/program.json`에 복사한다. 복사 직후 bytes/cmp로 동일함을 확인한다.
3. 같은 시작 PR에서 `approval_pointer` 하나만 실제 대표님 범위·시작 결정 기록 링크 하나로 바꾼다. 나머지 필드·nodes·spec·선행·grade·flag는 바꾸지 않는다. 정의 변경이 필요하면 앞서 별도 초안 개정 PR에서 검토·승인을 다시 받는다.
4. 시작 PR의 최종 exact HEAD로 독립 Fable 감사를 받고 대표님이 병합한다. 감사 뒤 파일을 바꾸면 새 exact-HEAD 감사가 필요하다. 시작 PR은 정책 C 자동 병합 대상이 아니다.
5. 중앙 최종 source의 실제 채택·byte-identical 보호된 설치·service authorization·host qualification/activation과 runtime attestation을 확인한 뒤에만 중앙에서 task를 materialize/start한다. 호스트 원장·MAC·승인 receipt를 제품 PR에서 만들지 않는다.

시작 PR의 pointer 변경 외 byte 차이가 없음을 확인한다. 현재 v1 reader가 무조건 거부하는 root `registration_scope` 실행 표시는 비실행 초안에서 제거하고 범위 manifest를 문서로 보존했다. 안전 경계는 실행 경로의 부재와 PENDING pointer다. manifest는 runtime 승인을 자동 판정하는 새 API가 아니며, 시작 승인자는 전체 범위를 명시적으로 확인한다.

## 해시 규칙

manifest 경로는 `docs/aiops/REGISTRATION_SCOPE_DRAFT.json`이다. plan 정의는 `approval_pointer`와 과거 `registration_scope` root만 제외한 JSON 전체를 UTF-8·key 정렬·compact separators·ensure_ascii=false로 직렬화해 SHA256을 계산한다. pending catalogue와 Finance 부분집합은 전체 JSON에 같은 규칙을 적용한다. 각 node도 전체 정의 digest를 기록한다. 정의 문서는 실제 바이트 SHA256이다. manifest 자신의 digest·승인 source commit은 외부 승인 기록에 남겨 순환을 피한다. 정의/선행/flag/문서가 달라지면 manifest를 다시 계산하고 현재 HEAD를 검토한다.

## 중앙 채택 검토 대상

[중앙 #47](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/47) 최종 source HEAD `09e161caa652d75e9617caf632b3b9899be35740` 하나다. `5d2e97a`의 Fable FAIL을 반영한 source 검증 후손이며 새 독립 Fable PASS·설치·qualification·activation 증거는 아니다.

| checkpoint | 상태 |
|---|---|
| `a8b7355712c58de8d27c85a535fb241a09a4037c` | 과거 기록, 채택 대상 아님 |
| `94a768e19df12703ea0b9a49e49972feb2f6ef4f` | 과거 기록, 채택 대상 아님 |
| `e34868c6a12e5488224095b3248e59c8be9128f1` | 과거 기록, 채택 대상 아님 |
| `15f7fe67726c04efd5a6d289ef985041f40c511a` | 과거 기록, 채택 대상 아님 |
| `754fae0` | 과거 기록, 채택 대상 아님 |

대표님 정책 C 원문(2026-10-01): [#47 결정 기록](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/47#issuecomment-5927605393). 계약 변경 YES·RELEASE·user_merge는 대표님 병합이다. 노드별 판정은 [PROGRAM_ASTRA_DELEGATION.md](PROGRAM_ASTRA_DELEGATION.md)에 남겼다. 기존 금융/chain/과금/외부 전송/실사용/화면/작품/release 잠금과 UNKNOWN fencing을 유지한다.
