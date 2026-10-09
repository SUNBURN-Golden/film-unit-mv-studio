# ADR 0002 — 잠정 문서 타입 (ADR 0001에 없는 15종)

- 상태: 이미 구현된 문서 타입의 필드 기록. 비작성자(A3) 검토는 이 노드에서 수행되지 않았다. 검토 상태 PENDING. 독립 감사 PASS, runtime qualification, 작품 승인, release가 아니다.
- 관계: [ADR 0001 계약](0001-frame-animation-v1-contract.md)과 [스키마 명세](0001-frame-animation-v1-schemas.md)를 대체하지 않는다. 여기 타입은 0001 명세에 없다. 0001의 금지 문장과 이 문서가 어긋나면 0001을 따른다.
- 코드 표시: `engine/animation_schema.py`의 `PROVISIONAL_DOCUMENT_TYPES`. `check_document`는 각 타입의 `schema_version` 1만 읽는다.

## 1. 공통

새 모드 JSON은 CANON_JSON_V1(정렬 키, UTF-8, 끝 LF)이다. `document_type`과 `schema_version`이 둘 다 있어야 한다. `schema_version`이 없거나, 정수가 아니거나(bool 포함), 1이 아니면 거부한다. 알 수 없는 `document_type`도 거부한다.

LEGACY_MV 프로젝트는 이 문서를 만들지 않고, 기존 파일을 이 타입으로 해석하지 않는다. 이전 schema_version은 없다. 이미 디스크에 있는 version 1 바이트는 그 검증기로만 읽는다. 나중 버전은 새 `schema_version`과 새 reader이며, version 1 로그를 제자리에서 다시 쓰지 않는다. jsonl 기록은 append-only다.

## 2. 타입

각 행의 필수 키는 해당 검증기가 요구하는 정확한 키 집합이다. 소비자는 그 검증기와 그것을 호출하는 모듈이다.

| document_type | version | 경로 | 소비자 | 필수 키 |
|---|---|---|---|---|
| `animation_waves` | 1 | `production/waves.json` | `engine/animation_locks.py` `validate_waves` | `document_type`, `schema_version`, `waves`. 각 wave는 `wave`, `shots`, `difficulty`, 선택 `note`. difficulty 항목은 `type`, `reason`, 선택 `shots` |
| `composite_recipe` | 1 | `animation/shots/{shot_id}/composite_recipe.json` | `engine/compositor.py`가 기록. `check_document`는 타입과 버전만 검사한다 | 기록 키: `document_type`, `schema_version`, `shot_id`, `instance_id`, `plan_sha256`, `rig`, `layers`, `tracks`, `canvas`, `background`, `camera`, `color_path`, `filter`, `members` |
| `animation_work_packet` | 1 | `animation/packets/<shot>.json` | `engine/packets.py` `validate_work_packet` | `document_type`, `schema_version`, `shot_id`, `instance_id`, `plan_sha256`, `plan_revision`, `canvas`, `fps`, `used_source_range`, `unused_handles`, `segments`, `controls`, `inputs`, `requests`, `transport`, `import`, `warnings`, `notes` |
| `animation_draft_frames` | 1 | `animation/shots/{shot_id}/draft_frames.json` | `engine/animation_assets.py` `validate_draft_frames` | `document_type`, `schema_version`, `shot_id`, `entries`. entry: `frame`, `role`, `member`, `sha256`, `byte_length`, `source_name`, `imported_at`, `packet_sha256`, `references`, `asset_pin`, `state` (`DRAFT`) |
| `subscription_entitlement` | 1 | 사용권 스냅샷 (구독 worker가 기록) | `engine/execution_workers/subscription.py` `validate_entitlement` | `document_type`, `schema_version`, `entitlement_id`, `service`, `usage_path`, `account_binding`, `credential_epoch`, `official_execution_api`, `allowance`, `used`, `inclusion`, `prices`, `service_caps`, `observed_at`, `expires_at_ms`, `notes` |
| `subscription_packet` | 1 | 수동 패킷 파일 | `engine/execution_packets.py` `validate_subscription_packet` | `document_type`, `schema_version`, `packet_id`, `service`, `route`, `worker`, `execution_unit`, `plan`, `frame_contract`, `inputs`, `expected_output`, `verification`, `receipt`, `entitlement`, `limits`, `issued_at` |
| `subscription_result` | 1 | 패킷 결과 파일 | `engine/execution_packets.py` `validate_subscription_result` | `document_type`, `schema_version`, `packet_id`, `job_key`, `attempt_id`, `request_id`, `snapshot_digest`, `plan_revision`, `actor`, `worker_script_sha256`, `worker_script_version`, `session_epoch`, `frame_range`, `outputs`, `receipt_nonce`, `grant_digest`, `state`, `error`, `produced_at`, `environment` |
| `capability_measurement` | 1 | 측정 기록 | `engine/animation_schema.py` `validate_capability_measurement` | `document_type`, `schema_version`, `measurement_id`, `evidence_id`, `evidence_digest`, `input_digest`, `quality_digest`, `scope`, `cold`, `warm`, `stage_timeline`, `shared_edge`, `peaks`, `transfer`, `cache`, `manual`, `usage`, `samples`, `observed_at` |
| `replay_packet` | 1 | replay doctor가 쓰는 패킷 | `engine/replay_doctor.py` `validate_replay_packet` | `document_type`, `schema_version`, `packet_id`, `created_at`, `build`, `inputs`, `recipe`, `toolchain`, `deliverables`, `restore`, `credentials`, `reproduction`, `qualification`, `notes`. credential 값과 비밀 형태 키는 거부한다 |
| `w00_pilot` | 1 | `production/w00_pilots.jsonl` | `engine/w00_gate.py` `validate_pilot` | `document_type`, `schema_version`, `pilot_id`, `wave`, `target_kind`, `target_id`, `reason`, `source_sha256`, `sequence_sha256`, `artifact_sha256`, `capability_ref`, `manifest_ref`, `job_result_id`, `review_id`, `reviewer`, `reviewer_kind`, `delegation_id`, `decision`, `issues`, `recorded_at`, `binding_sha256` |
| `approver_delegation` | 1 | `production/approver_delegations.jsonl` | `engine/w00_gate.py` `validate_delegation` | `document_type`, `schema_version`, `delegation_id`, `delegator` (박준태), `delegate`, `scope`, `expires_at_ms`, `note`, `recorded_at` |
| `w00_spend_approval` | 1 | `production/w00_spend_approvals.jsonl` | `engine/w00_gate.py` `validate_spend` | `document_type`, `schema_version`, `approval_id`, `wave`, `decision_id`, `jobs`, `reason`, `approver`, `reviewer_kind`, `delegation_id`, `recorded_at` |
| `delivery_approval` | 1 | `production/delivery_approvals.jsonl` | `engine/w00_gate.py` `validate_delivery` | `document_type`, `schema_version`, `approval_id`, `build_id`, `deliverable`, `review_id`, `deliverable_sha256`, `build_manifest_sha256`, `frame_sequence_root`, `approver`, `reviewer_kind`, `delegation_id`, `decision`, `recorded_at` |
| `delivery_bundle` | 1 | 전달 묶음 `bundle.json` | `engine/delivery_package.py` `validate_bundle` | `document_type`, `schema_version`, `bundle_id`, `project_id`, `build_id`, `created_at`, `delivery_profile`, `profile_check`, `profile_problems`, `storage_profile`, `delivery_complete`, `approval_consistent`, `members`, `sources`, `qc`, `states`, `facets`, `work_contract`, `delivery_clock`, `meets_work_contract`, `notes`. `artwork_acceptance`는 PENDING, `external_publication`은 NOT_AUTHORIZED |
| `delivery_review_scope` | 1 | 묶음의 `review/scope.json` | `engine/delivery_package.py` `validate_scope` | `document_type`, `schema_version`, `build_id`, `film_review_state`, `film_review_id`, `deliverables`, `scope_match`, `compared`, `locks_are_not_approval`, `inherited_reviews` |

## 3. 마이그레이션

- 이 15종을 LEGACY_MV 검수, LOCK, Build 1, 가사 cue로 승계하지 않는다.
- version 1 기록을 version 2로 올리거나 필드를 조용히 추가하지 않는다. 필드가 이 표와 다르면 해당 검증기가 거부한다.
- `composite_recipe`는 작성기가 위 키를 기록한다. 별도 구조 검증기를 이 개정에 추가하지 않는다. 타입·버전 거부는 `check_document`가 담당한다.
- 비작성자 검토가 PENDING인 동안 이 타입은 잠정이다. 검토 PENDING을 자격 완료나 작품 승인으로 읽지 않는다.
