# FRAME_ANIMATION_V1 — 전송 경로·seekable pack·완료 근거 고도화 설계

공통 후속 규약: [중앙 #46](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/46), 후보 HEAD `a8b7355712c58de8d27c85a535fb241a09a4037c`. [고정 설계](https://github.com/BeautifulMind-JT/ai-ops-control-plane/blob/a8b7355712c58de8d27c85a535fb241a09a4037c/engineering/docs/PROGRAM_EXECUTION_EVOLUTION_DESIGN_KO.md)는 아직 운영·승인 evidence가 아니다.

작성일: 2026-09-30 KST  
설계 상태: **후속 후보, DESIGN_ONLY / PENDING_APPROVAL_DO_NOT_DISPATCH**  
고정 선행: 설계 #19 `c66a90cedb4ef0da5719b69c9771da0b46fd7868`, 등록 #20 `c716bce521bb897b6196b0111f9615973a00584e`

이 문서는 User가 요청한 설계 고도화와 검토용 PR 작성의 결과다. 기존 감사 대상 HEAD를 이동하지 않고 그 위의 후속 후보로 준비한다. 제품 코드·실제 Drive/worker·추가 과금·credential·호스트·activation·작품 승인·벤치마크를 실행한 증거가 아니다. 채택 전에는 revision 3과 이 후보 사이의 변경을 운영 계약으로 소비하지 않는다. 독립 exact-HEAD 검토와 적용되는 사용자 채택·병합 게이트가 필요하다.

범위는 [실행·저장 설계](FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md)의 데이터 이동·pack 읽기 계약과 완료 증거를 구체화하는 것이다. 연속성 검토에서 발견한 ANIM-010 이후의 늦은 B adapter UI 통합은 기존 ANIM-012의 명시적 책임으로 보완한다. 원곡·가사·frame/PTS·색·LOCK·최종 사람 승인·UNKNOWN fencing·bounded retry·독립 encode 검사와 별도 권한 경계는 유지한다. 기술 검사를 예술적 승인으로 바꾸지 않는다.

## 1. 정본·소비자와 채택 순서

| 계약 | 이 후보의 소유 절 | 소비자 |
|---|---|---|
| 데이터 이동과 자원 계산 | 2절 | ExecutionPlan·scheduler·coordinator·workspace·capability probe |
| pack/index와 구간 검증 | 3~4절 | archive writer/reader·Drive/local backend·restore·cache |
| 완료 facets와 실제 수용 근거 | 5절 | program 집계·qualification 보고·제품 UI·milestone 검토 |
| 수용 fixture와 증거 | 6~7절 | ANIM-001/012/013/014/017/018 구현자·독립 reviewer |

모듈 및 schema 이름은 구현 대상이다. v1의 `ExecutionPlan 1`·`StorageArchive 1`·`CapabilityEvidence 1`을 구현 전 ANIM-001에서 아래 필드까지 확정한다. 이미 배포된 소비자가 있으면 별도 명시적 migration/지원 버전을 정하고 무조건 덮어쓰지 않는다. 이번 후보만으로 데이터 버전을 올리거나 제품을 변경하지 않는다.

중앙 선행의 검토 후보는 #44/#45의 보호 브리지와 후속 설계를 통합·보완한 [ai-ops-control-plane #46](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/46), 고정 HEAD `a8b7355712c58de8d27c85a535fb241a09a4037c`이다. #44를 별도로 병합하는 경로는 전제가 아니다. 후속 설계 문서의 채택은 중앙 구현·host qualification·attestation 완료가 아니다. 중앙 문서 경로를 실제 승인 commit에 pin하기 전에는 이 pointer가 운영 증거가 될 수 없다.

순서는 선행 설계/등록과 해당 중앙 기능의 채택·qualification을 확인하고, 이 후속 후보를 exact HEAD로 검토해 채택한 뒤, 최종 승인 plan commit의 task에서 계약을 구현하는 것이다. `.aiops/program.json`의 PENDING과 중앙 #46 후보 포인터는 실제 채택·qualification commit 및 근거가 확정될 때만 갱신한다. 이 bootstrap PR은 스스로 프로그램의 자동 병합 대상이 되지 않는다.

## 2. 기본 경로: coordinator 중개를 데이터 DAG로 표현

### 2.1. 경로 종류와 eligibility

| 경로 | 데이터 이동 | 상태/조건 |
|---|---|---|
| `COORDINATOR_RELAY` | Drive/local archive → coordinator → worker → coordinator → archive | 현재 새 모드의 기본 구현 후보. 사용자 Drive OAuth는 coordinator 경계에만 존재 |
| `MANUAL_PACKET` | coordinator가 검증한 제한 packet → 사용자의 공식 notebook/browser 동작 → 검증 import | 실제 packet 한도·사람 동작·반환 형식이 probe된 범위에 한함 |
| `DIRECT_DRIVE` | worker가 자체 Drive 권한으로 원격 객체를 읽고 씀 | **기본 후보 집합에서 제외**. 별도 ADR·사용자 권한 결정·인증 및 scope qualification 후의 후속 범위 |

worker에 job/object/범위/만료로 제한한 전송 권한을 주는 것과 worker에 사용자 Drive token을 주는 것은 다르다. 기본 relay의 worker endpoint는 고정 바이트를 받거나 내보내며 사용자 OAuth/refresh token·Drive 계정 권한을 받지 않는다. 임시 전송 권한은 기존 보안 계약대로 회수하고 receipt에는 비밀 없는 binding만 남긴다. `DIRECT_DRIVE`라는 이름·네트워크 접근 probe만으로 별도 권한 결정이 충족되지 않는다.

기본 coordinator의 위치는 `USER_DESKTOP`으로 명시한다. `PROTECTED_REMOTE_RELAY`는 이미 허용·검증한 별도 중개 배포가 있을 때만 다른 위치로 계획할 수 있다. 이번 설계는 원격 중개 서비스 배포를 승인하지 않는다. coordinator가 PC에 있으면 stream 방식으로 PC 디스크 적재를 줄일 수 있어도 입력/출력의 WAN 바이트는 PC를 통과한다. 이를 ‘PC를 거치지 않는 Drive 직접 처리’로 표시하지 않는다.

### 2.2. ExecutionPlan의 transfer edge

고정 plan은 storage/runtime/encoder 선택 외에 `transfer_route`, `coordinator_location`, `transfer_edges[]`와 `resource_reservations[]`를 포함한다. edge의 최소 계약은 다음과 같다.

| 필드 | 의미 |
|---|---|
| `edge_id`, `from_role`, `to_role` | DAG의 유일 edge와 실제 송수신 위치. archive/coordinator/worker 역할과 계정 비밀 없는 위치 ID |
| `snapshot_digest`, `object_digest`, `index_digest` | 고정 입력/출력 및 pack index binding. 파일 이름·최신 file ID로 대신하지 않음 |
| `member_ids`, `byte_ranges` | 필요한 전체 멤버와 halo. 범위는 0-based `[offset, offset + length)` |
| `verification` | 수신 완료 후 member hash 또는 full-pack hash의 요구. 전송 완료와 검증 완료를 구분 |
| `max_inflight_bytes`, `spool_limit_bytes` | 송신·수신·검증 읽기 및 실패 복구 중간물의 hard cap |
| `evidence_ref`, `measured_at` | 같은 경로/환경의 throughput·latency·request 한도 근거. 없는 값은 UNKNOWN |
| `credential_boundary`, `expiry_binding` | 비밀 없는 권한 소유 경계 및 job-scoped 전송 권한 만료/회수 binding |
| `dependencies`, `completion_receipt_ref` | 검증/소비 순서와 실제 edge 완료 근거. receipt 자체는 실행 중 생성 |

계획에 receipt 자리만 있어도 실행 완료로 집계하지 않는다. 선언한 byte range는 실제 요청 바이트와 구분하고, retry가 발생한 실제 전송은 별도로 모두 계수한다. buffer ownership·GPU fence·UNKNOWN/cancel은 기존 WorkerProtocol/FrameStream을 소비한다.

### 2.3. 자원·시간 계산

relay는 Drive read와 worker upload를 각각 별도 네트워크 edge로 모델링한다. 출력도 worker download와 archive write를 구분한다. 두 edge가 같은 PC 링크를 공유하면 개별 peak throughput을 동시에 사용할 수 있다고 가정하지 않는다. decode/compose/encode/검증의 CPU·GPU·디스크 경합과 bounded queue의 backpressure를 같은 DAG에 넣는다.

각 위치의 peak 예약은 download chunk + 검증용 보관 bytes + decode buffers + pinned cache + prefetch + output spool + mux temporary + 검사 read buffers의 동시 생존 구간으로 계산한다. pack 전체 수용이 필요한 fallback의 용량도 별도 예약한다. 압축 PNG의 바이트와 decode 후 RGBA 크기를 서로 대체하지 않는다. PC/worker cap 각각을 충족해야 하고 작은 cap을 이유로 원본·프레임·품질을 자동 삭제/축소하지 않는다.

보고는 `archive_read_bytes`, `relay_to_worker_bytes`, `worker_to_relay_bytes`, `archive_write_bytes`, `verification_read_bytes`, 실제 request 수를 분리한다. 같은 bytes를 서로 다른 edge에서 계수하는 것은 실제 이동 비용이며 총량을 하나의 입력 크기로 축약하지 않는다. stage overlap은 critical path로 계산하고 네트워크/자원 상한을 공유한다. 구현 전 속도 향상 비율이나 특정 완료 시간을 약속하지 않는다.

## 3. Seekable pack/index v1 후보

### 3.1. 컨테이너와 identity

v1 후보는 전체 바이트가 고정된 binary pack에 독립 PNG 멤버의 원래 바이트를 명시한 순서로 이어 담고, **outer compression을 사용하지 않는다**. pack 바이트는 header + PNG bodies뿐이며 index는 pack 밖의 **별도 sidecar 객체**다. header의 magic/format version과 멤버 body 영역을 ANIM-001에서 고정하고 header에 자기 pack hash나 sidecar index hash를 넣지 않는다. ZIP/TAR를 실행 가능한 입력으로 사용하거나 압축을 여러 겹 해제하는 계약이 아니다. archive manifest는 header+PNG bodies의 pack 전체 SHA256/byte length와 sidecar index SHA256을 각각 pin한다. sidecar index에는 pack SHA256을 넣을 수 있지만 index bytes는 pack hash 입력에 들어가지 않고 index의 자기 digest도 자기 입력에 넣지 않는다. 따라서 pack→index→pack의 순환 hash를 만들지 않는다.

같은 멤버·순서·format version은 동일 pack bytes를 만든다. content-addressed 생성은 overwrite가 아니라 새 object다. 수신 backend의 ID/revision은 transport locator이며 내용 identity는 hash다. pack hash를 가진 index가 있다는 사실만으로 실제 전체 pack을 읽어 검증했다고 주장하지 않는다.

### 3.2. index 필수 내용

| 필드 | 계약 |
|---|---|
| `pack_format_version`, `pack_sha256`, `pack_byte_length`, `header_byte_length` | 고정 container의 전체 identity와 bounds |
| `snapshot_digest`, `recipe_digest`, `sequence_digest` | 사용/생성한 snapshot과 순서·내용 관계. 검수 approval digest는 sequence digest에 섞지 않음 |
| `members[]` | 고정 순서. 각 entry에 member ID·global frame index 또는 source 역할·byte offset·byte length·PNG SHA256 |
| `image_contract` | width/height·pixel format·alpha/color 정책·허용 encoded/decoded byte 상한 |
| `member_count`, `frame_coverage` | frame index의 끝 제외 범위 및 예외 없는 coverage. halo/source 멤버는 전달 프레임 수와 별도 |
| `retention_refs` | 원본/채택/완료 빌드 참조. cache eviction 권한과 구분 |

index는 bounded size로 읽고 archive가 pin한 index hash를 먼저 검증한다. 이후 정수 offset/length의 overflow·pack bounds·header 침범·overlap·중복 member ID/전달 frame index·순서·누락을 검사한다. 계산 파라미터는 제한된 숫자/문자열만 허용하며 경로·명령·코드를 실행하지 않는다. 이미지 metadata도 허용된 크기·format·color/alpha 계약을 확인한 뒤 decoder를 호출한다.

### 3.3. range-read 검증 단계

1. 고정 archive manifest와 index의 binding을 확인한다.
2. 필요한 전체 멤버와 halo의 byte range를 만들고, cap 안에서 인접 범위만 coalesce한다. 요청 수를 줄이려고 무관한 큰 범위를 자동으로 모두 읽지 않는다. 실제 read amplification을 기록한다.
3. range 지원 backend의 응답은 정확한 requested range·반환 byte 수·고정 전체 length를 검증한다. mutable locator가 다른 revision/길이를 가리키면 기존 snapshot에 붙이지 않는다.
4. 멤버 전체 바이트의 SHA256을 index와 대조한 뒤 DECODE/COMPOSE에 넘긴다. 임의 부분 PNG의 hash나 HTTP 성공을 전체 member 검증으로 사용하지 않는다.
5. 동일 member를 여러 byte request로 받으면 완전한 bytes를 bounded workspace에서 조립/검증한다. 외부 제공자의 multipart 응답은 실제 구현/probe된 형식만 소비한다.
6. full-pack SHA256 검증은 전체 pack을 모두 읽었을 때만 기록한다. 부분 읽기 receipt는 `VERIFIED_MEMBERS`와 검증한 정확한 member/range를 기록하고 `VERIFIED_FULL_PACK`으로 승격하지 않는다.

압축 PNG는 outer compression이 없더라도 decode 확장 위험이 있다. PNG signature/header·허용 차원·픽셀 수·bit depth/format·encoded length를 읽기 상한 안에서 검사한다. decoder는 계산한 decoded byte cap과 RAM 예약을 지켜야 한다. 잘못된 header·비정상 확장·손상·초과 allocation은 해당 member를 거부하고 원본을 지우지 않는다.

## 4. Range 미지원·중단·restore

### 4.1. fallback은 명시적 계획 분기

backend가 Range를 지원하지 않거나 requested Range에 전체 응답을 반환하면 기본 동작은 멤버 구간 성공으로 처리하는 것이 아니다. full response를 버퍼링하기 전에 전체 pack 수용 용량/전송 allowance/시간 근거를 검사한다. hard cap을 넘는 whole-pack 응답은 stream을 중단하고 `RANGE_UNSUPPORTED` 또는 `CAPACITY_BLOCKED`로 보고한다. 기존 raw response를 무한 임시 파일로 받지 않는다.

허용된 동일 backend에서 전체 pack 다운로드가 cap·권한·사용량을 충족하면 ExecutionPlan의 명시적 `WHOLE_PACK_VERIFIED` 분기를 사용하고 전체 SHA256을 검증한다. 이 분기는 실제 요청/전송량과 peak 공간을 보고한다. 작은 독립 pack으로 다시 계획할 때는 고정 멤버/sequence/recipe가 같아야 하고 storage locator/index 변경을 새 archive revision으로 기록한다. 기존 완료 빌드의 manifest를 고치지 않는다. 유료 경로/provider/계정을 자동 변경하지 않는다.

잘못된 `Content-Range`, 짧은/긴 payload, 고정 total length 불일치, 범위 오류는 성공 fallback이 아니라 `INPUT_MISMATCH`다. backend의 416 응답 등을 최신 object로 조용히 갈아타는 근거로 사용하지 않는다. 원인이 미지원인지 snapshot 불일치인지 구분할 수 없으면 확인을 기다린다.

### 4.2. 이어 읽기와 cache

검증이 끝난 멤버를 content digest로 재사용한다. 검증되지 않은 일부 바이트는 verified cache로 공개하지 않는다. 다운로드 중단 뒤 같은 고정 pack/index로 붙일 수 있는 partial bytes의 bounds·length·receipt를 먼저 확인하고, 완료 멤버 hash를 재검사한다. 해석 불명 입력은 새 snapshot을 만들어 덮어쓰지 않는다. 재개 동작/시도 제한은 기존 명시적 bounded retry 계약을 따르며 standing polling·자동 무한 retry를 추가하지 않는다.

### 4.3. bounded restore

restore는 archive의 순서/coverage를 확인하고 `F_000001.png`부터 지정 끝까지 상대 basename을 앱이 생성한다. index의 경로 문자열을 그대로 filesystem 목적지로 신뢰하지 않는다. 출력은 허용 root의 임시 경로에 write→hash/형식 검증→확정 순으로 공개한다. symlink·절대/상위 경로·기존 승인 output overwrite를 거부한다. 전체 offline restore 공간 예약이 없으면 online 범위 읽기를 offline 재현으로 표시하지 않는다.

restore 중단은 새 출력 revision을 부분 상태로 남기고 완료 manifest를 공개하지 않는다. 완전한 멤버 coverage·바이트 hash·기술 검사가 끝난 후만 복원 완료를 기록한다. archive/cache/restore workspace의 삭제 권한은 기존 계약대로 분리한다.

## 5. 개발·자격·수용·릴리스의 독립 facets

한 상태의 COMPLETE로 개발 병합·실제 runtime qualification·작품 검수·공개 배포를 모두 표시하지 않는다. 중앙 후보의 다음 facets를 같은 의미로 소비한다.

| facet | 허용 상태 | 근거와 제한 |
|---|---|---|
| `node_state` | `NOT_STARTED / WAITING / IN_PROGRESS / DONE` | 중앙 신규 query의 정규화 projection 후보이며 현재 operation별 status enum을 바꾸지 않는다. `DONE`은 해당 current node 정의의 delivery가 host-pinned 근거로 exact-head 리뷰/조건을 통과하고 유효한 target에 실제 merge됐을 때만. 설계 문서·builder 완료 문장·fake 실행·단순 PR open으로 DONE 생성 금지 |
| `qualification_state` | `NOT_REQUIRED / UNQUALIFIED / PARTIAL / QUALIFIED` | operation·route·format·frame 범위·device/session·실제 접근·현재 증거에 따른 capability. scope 밖 확대 금지 |
| `acceptance_state` | `NOT_REQUIRED / PENDING / ACCEPTED / REJECTED` | 해당 gate의 지정 검수와 고정 artifact binding. 작품의 ACCEPTED는 실제 승인자/위임자의 검토를 요구 |
| `release_state` | `NOT_AUTHORIZED / NOT_RELEASED / RELEASED` | 별도 배포/공개 권한과 실제 release 근거. merge나 acceptance로 권한을 추론하지 않음 |

node facets는 host-pinned delivery/중앙 집계 계약이고 제품 worker job state·build seal state를 대체하지 않는다. Build의 ENCODED/UPLOADED/FINAL_CANDIDATE_READY는 그 자체로 `node_state=DONE`이나 작품 ACCEPTED를 만든다는 뜻이 아니다. 완료 manifest도 별도 release 권한을 만들지 않는다.

`NOT_REQUIRED`는 승인된 node 목적이 해당 자격/수용을 요구하지 않는 경우만 사용한다. 필요한 실제 환경이 없다는 이유로 UNQUALIFIED/PENDING을 NOT_REQUIRED로 바꾸지 않는다. 증거는 approval plan revision·delivery/source HEAD·fixture/input/recipe·runtime/driver/route·artifact hash·시각·검증된 scope와 연결한다. 관련 환경·입력·도구가 바뀌면 기존 증거를 적용 불가로 표시하고 새 현재 근거를 요구한다.

ANIM-013/014/015/016의 protocol 구현·fake 회귀는 개발 증거로 기록할 수 있다. 실제 Drive/remote/native/subscription 환경이 없으면 필요한 qualification은 UNQUALIFIED 또는 검증된 일부 scope만 PARTIAL이며 그 이유를 남긴다. 아직 중앙이 facets를 구현하지 않았으면 작성자가 host record를 조작해 채우지 않고 draft qualification 표에만 미완료를 보고한다.

ANIM-018의 원격 수용 완료는 **실제로 허용된 한 경로**에서 240초·24fps·1080p 입력→relay/worker→verified PNG/MP4→archive→replay/restore와 해당 UI·실패 fixture를 검증한 근거가 필요하다. 후보 driver/service 전부를 검사했다고 일반화하지 않는다. 환경이 없으면 코드 개발 범위가 병합돼도 원격 수용은 PENDING이며 프로그램의 목표 수용 완료를 선언하지 않는다. 실제 작품의 컷/전체 정상속도 검토·최종 승인과 공개 release는 이 합성 qualification과 독립이다.

## 6. 구현 수용 fixture

다음은 개발 시 구현할 검사 계약이며 이번 문서 작성에서 실행한 제품 테스트가 아니다.

| ID | 고정 상황 | 통과 근거 |
|---|---|---|
| `ROUTE-RELAY` | 같은 input/quality로 relay edge의 단계적 지연과 공유 PC 링크 경합 | 기본 경로가 DIRECT_DRIVE로 바뀌지 않음; edge별 bytes/request와 stage timeline·각 cap을 실제 기록 |
| `ROUTE-ELIGIBILITY` | worker는 network 가능하나 별도 Drive ADR/권한 증거 없음 | DIRECT_DRIVE 제외; 사용자 OAuth/refresh token이 packet/worker/receipt에 없음 |
| `PACK-SPARSE` | 세 독립 pack, 컷 하나 변경 및 양쪽 halo, 나머지 cache warm | 필요 멤버/halo만 해시 검증하여 읽음; 실제 요청 범위·전송/재사용 bytes·amplification 보고; 불필요 멤버 재decode 없음 |
| `PACK-TAMPER` | pinned index의 offset/length/순서/hash 및 pack 한 멤버 변조 | index hash/bounds/coverage/member hash의 정확한 단계에서 거부; 부분검증을 full-pack 검증으로 표기하지 않음 |
| `PACK-HTTP` | 206 정확 범위, Range에 200 전체, 잘못된 total/짧은 payload/416 | 정상 범위만 수용; 명시적 whole-pack 분기는 사전 cap 허용+full hash; mismatch와 미지원 분리 |
| `PACK-EXPANSION` | 작은 encoded PNG가 cap 밖 차원/decoded bytes를 선언 | decoder 초과 allocation 전에 거부; RAM/디스크 peak 상한 유지 |
| `PACK-INTERRUPT` | 멤버 중간 read·restore 중단, 한 검증된 멤버 cache 유지 | 같은 identity 재개; 검증된 멤버 재사용; incomplete bytes/coverage로 완료 공개 없음 |
| `RESTORE-BOUNDARY` | 파일명/path/symlink 공격·기존 승인 output·공간 부족 | 앱 생성 basename+허용 root만 사용; 승인 output/원본 유지; offline 완료 오표시 없음 |
| `FACETS-NO-RUNTIME` | fake worker PASS와 merged 개발 delivery, 실제 remote 접근 없음 | host 근거 있으면 개발 DONE 가능; qualification UNQUALIFIED, 수용 PENDING, release NOT_AUTHORIZED 유지 |
| `FACETS-REAL-SCOPE` | 실제 한 허용 route의 고정 통합 fixture와 실패 주입 | 검사한 scope만 QUALIFIED; 실제 수용 evidence로 해당 gate ACCEPTED; 예술적 승인·release 자동승격 없음 |
| `UI-LATE-ADAPTER` | 011이 010보다 먼저 비활성 연결로 병합된 뒤 두 결과를 012에서 통합 | [개발 안내의 ANIM-012 수용](FRAME_ANIMATION_V1_DEVELOPMENT_KO.md)의 UI-B-READY/INPUT-QUOTE/UNKNOWN/CANCEL-RACE를 실행; fake로 늦은 활성 연결·오류·fence를 검증하고 실제 provider 호출 0 |

성능 수용은 동일 input/quality·cold/warm·실제 network/device 조건에서 edge별 바이트와 요청수, stage/전체 완료 시간, peak PC/worker disk/RAM/VRAM·spool·검증 읽기, 중단 후 복구를 보고한다. 반복 측정의 변동과 수동 동작 시간을 포함한다. 구현/실측 없는 배수·최소 용량·완료 시간을 약속하지 않는다.

## 7. 기존 node에 연결할 증거

| node | 추가 명세·완료 증거 |
|---|---|
| ANIM-001 | 2절 transfer edge/위치·3절 pack/index·5절 facets의 schema/소비자/migration 고정. 권한 없는 DIRECT_DRIVE 제외 |
| ANIM-012 | 병합된 010/011의 늦은 B adapter UI 연결과 UI-LATE-ADAPTER fixture. 기존 240초·두 모드·세 OS 패키지 수용도 유지; fake UI 증거를 실제 provider/작품 승인으로 승격하지 않음 |
| ANIM-013 | seekable pack 작성/읽기·index/member/full hash 구분·미지원 fallback·bounded restore·PACK/RESTORE fixtures |
| ANIM-014 | 실제 기본 relay edge·비밀 없는 receipt·resource reservation·backpressure·ROUTE fixtures. 기존 UNKNOWN/cancel fence 유지 |
| ANIM-017 | relay의 실제 이동과 경합까지 critical path에 포함; sparse 재컴파일의 request/bytes/decode amplification 실측 |
| ANIM-018 | 실제 허용된 route의 통합 수용 evidence와 네 facets 보고. 개발 merged와 실제 qualification/작품 approval/release 구분 |

node ID·DAG·audit floor·milestone·LOCK·사용자 별도 결정은 유지한다. 이 후보 명세는 실행 증거가 아니므로 PENDING을 제거하거나 release/서비스 자격을 만들어내지 않는다.

중앙 bootstrap의 현 채택 검토 후보는 #44/#45를 통합·보완한 [#46](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/46)이다. 기존 #44 감사의 DECISION_REQUIRED를 통과한 것으로 간주하지 않는다. PA-1 권한 예외는 PENDING이며, 보호된 reconcile과 실제 host qualification 전에는 전체 실행 NOT_READY다. 기존 중앙 포인터는 이전 체크포인트 기록이고 최종 승인 registration에는 실제 채택·qualification commit을 pin해야 한다.

## 8. 제품 계약 고도화 — 2026-10-01 후보

이번 후속은 transfer/pack 문서만 추가하는 단계에서 더 나아가 정본 상세 설계·실행/저장 설계와 `.aiops/program.json`을 함께 바꾼다. 기존 ANIM-001~018의 ID·범위·선행은 유지하고, 아래 5개 후속 node를 새 승인 plan revision의 23개 개발 분모에 포함한다. 기존 실행 중 plan이나 host 등록을 자동 갱신하지 않으며 전체 closeout의 추가 node는 기존 bootstrap 기능 구현을 선행에서 막지 않는다.

| 보강 계약 | 정본 위치 | 구현 연결 |
|---|---|---|
| 계정/credential epoch·relay grant 범위·upload session 비밀 | 실행/저장 3.1 | 013 선행 schema, 019 자격 갱신, 021 중단/재연결 |
| runtime/encoder/route/사용권 scope·현재 증거·실측 | 실행/저장 7.2.1 | 019; LLM 구독과 실제 compute/API/encoder 권한 구분 |
| canonical source/recipe/clean/subbed/encode provenance·invalidate closure | 상세 설계 11.5, 실행/저장 8.2.1 | 001 ADR, 017 interface, 020 cold/warm selective 검증 |
| submit journal·verified checkpoint·upload progress와 archive seal 분리 | 실행/저장 5.4/9.1 | 014 선행 schema, 021 crash/응답 손실·UNKNOWN fence |
| 어려운 본편 W00의 KEEP/CHANGE/MIX·실제 전체 정상속도 재생/최종 파일 승인 | 상세 설계 10.5 | 007 첫 흐름, 022 늦은 integration; 작품 승인자는 박준태/명시적 위임자 |
| 23-node 전체 개발/실제 목표 수용 분리 closeout | 개발 안내 ANIM-019~023 | 023은 001~022 모두 의존; 기존 018 baseline만으로 전체 닫기 금지 |

각 추가 node의 구체 산출물·fixture·선행·A3/A2·소유 책임은 [개발 안내](FRAME_ANIMATION_V1_DEVELOPMENT_KO.md)의 후속 표와 executable plan spec에 명시한다. 분모 23은 개발 scope count이며 actual provider qualification·작품 approval·release count가 아니다. 예술적 수용은 합성 240초, fake worker·CI PASS로 대신하지 않으며 실제 W00/최종 출력이 없으면 PENDING이다. 실제 자원 부재를 NOT_REQUIRED로 면제하지 않는다.

중앙 [#47](https://github.com/BeautifulMind-JT/ai-ops-control-plane/pull/47), source HEAD `94a768e19df12703ea0b9a49e49972feb2f6ef4f`는 #46 위의 실패 증거/한도 재개 구현 후보이며 아직 설치·host qualification 근거가 아니다. 그 보호된 Fable audit 재개를 제품의 generation/worker/upload 재제출 권한으로 전이하지 않는다. 이 제품 plan의 채택·actual runtime/사용권 qualification·User 작품 검토와 중앙의 formal 감사·host 적용은 각각 증거를 요구한다.
