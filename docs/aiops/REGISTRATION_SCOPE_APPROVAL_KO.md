# 전체 등록 범위 승인 경계 — 후보

이 문서와 `.aiops/registration-scope.json`은 전체 후보 범위를 검토하기 위한 기록이다. 승인·독립 감사·host 설치·qualification·activation은 PENDING이다. 기존 설계의 승인이나 원래 roadmap 참조만으로 뒤에 추가한 정의를 채택하지 않는다.

## 승인 단위

등록 승인은 해당 저장소의 전체 plan 정의와 모든 pending catalogue를 함께 대상으로 삼는다. 노드 ID·명세·선행·외부 선행·감사 등급·게이트·User-only·자동 병합 표시·전체 완료 분모가 모두 포함된다. 일부 단계나 예전 HEAD의 PASS를 나머지 새 정의에 전이하지 않는다. Finance 정의는 protocol catalogue 안의 동일 ID·정의 digest를 참조하는 부분집합이며 작업 수에 다시 더하지 않는다.

manifest의 source plan digest는 UTF-8 JSON을 key 정렬·공백 없는 구분자·비 ASCII 보존으로 직렬화한 뒤 SHA256을 계산한다. `approval_pointer`와 `registration_scope`만 제외하며 나머지 root 필드와 전체 nodes를 포함한다. pending catalogue와 Finance 부분집합은 전체 JSON 정의를 같은 방식으로 계산한다. manifest 자신의 digest와 실제 승인 대상 commit은 외부 검토·승인 기록에 남겨 해시 순환을 피한다. 정의나 선행·grade·flag가 바뀌면 새 manifest와 현재 HEAD의 재검토가 필요하다.

## 활성화 전 조건

실제 User 범위·시작 결정은 전체 manifest digest, 정확한 source commit, 각 plan/catalogue digest와 해당 정의를 대상으로 한 독립 감사 근거를 결합해야 한다. 부분 선택은 실제로 승인된 적용 범위 개정으로 기록하며, 제외된 정의를 자동 DONE 처리하지 않는다. DEFERRED는 HOLD와 분모에 남고, DECLINED 정리는 User 병합의 적용 범위·계획 개정과 기존 분모·영향받은 정의의 기록을 요구한다.

확장된 후보 plan에는 `registration_scope` 표시와 PENDING `approval_pointer`가 함께 들어간다. 중앙 후보 reader는 `registration_scope`가 있으면 값이 PENDING·APPROVED·잘못된 형식인지에 관계없이 dispatch를 거부한다. 단순 상태 문자열 변경이나 기존 승인 pointer 대입으로 이 후보를 실행할 수 없다. 실제 전체 범위 승인 증거를 검사하는 reader의 별도 명시적 채택·독립 검토·qualification과 승인된 등록 개정 전에는 실행 후보를 활성화하지 않는다. 이 변경은 그런 새 승인 API나 보호된 receipt 발급기를 구현하지 않는다.

표시 삭제는 승인 경계 변경이며, 전체 범위 결정을 반영하는 별도 User 승인 등록·정책 개정으로만 다룬다. 현재 공유 GitHub 자격증명에서 권한 있는 작성자의 악의적 표시 삭제까지 이 작은 source guard가 막는다고 주장하지 않는다. 보호된 host 권한·원장·승인 proof의 추가 강제는 따로 채택해야 한다.

## 보존하는 경계

기존 User-only·금융·chain·실사용·외부 전송·과금·공개 운영·화면 승인·release 제한을 유지한다. 코드의 DONE, 실제 qualification, User acceptance, release는 별개의 판정이다. 외부 서비스나 승인 증거가 없으면 해당 작업과 후속 readiness를 HOLD하며, unknown을 성공이나 실제 자격으로 승격하지 않는다. 등록 PR과 이 manifest는 자동 병합 대상 program delivery가 아니다.
