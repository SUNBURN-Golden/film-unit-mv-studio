# ADR 0001 — FRAME_ANIMATION_V1 계약

- 상태: ANIM-001 구현 계약. 제품 설치, runtime qualification, 작품 승인, release, 독립 감사 PASS가 아니다.
- 태스크: [FILM-ANIM-001](https://github.com/BeautifulMind-JT/film-unit-mv-studio/issues/26), plan commit `e7a162aca94e868b4ffc6c9181ef35ca38656b7e`
- 승인 포인터: [ai-ops-control-plane #58](https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/58)
- 스키마: [0001-frame-animation-v1-schemas.md](0001-frame-animation-v1-schemas.md). 두 문서는 하나의 계약이다.
- 고도화 설계 pin: `docs/FRAME_ANIMATION_V1_EVOLUTION_KO.md` blob `5b2eb6224db8c4b18a547d981ecdfe9be0aa92e9` at the plan commit. 1~5절을 이 계약에 넣는다. 중앙 `09e161caa652d75e9617caf632b3b9899be35740`는 운영 pin이 아니다.
- 소비자 확인 기준: 같은 plan commit의 `engine/schema.py` blob `f891ebfc976c4269b3c4b03c7034791d452b41a0`

필드가 스키마 명세에 없으면 구현하지 않는다. 이 ADR의 금지 문장과 스키마가 어긋나면 금지 문장을 따른다. 채택된 금지와 다른 구현이 필요하면 `DECISION_REQUIRED`로 멈추고, 문서 작성자가 그 결정을 대신하지 않는다.

## 1. 소유 경로

| 문서 | 소유하는 것 | 소유하지 않는 것 |
|---|---|---|
| [ARCHITECTURE.md](../../ARCHITECTURE.md) | 구현된 v0.3 / LEGACY_MV. scope exception 절은 새 모드 예외의 포인터 | 프레임 편집·Drive·worker의 상세 필드 |
| [PROJECT_SPEC.md](../../PROJECT_SPEC.md) | v0.3 Preview/Final, Build 1, 예산. development contract 절은 개발 요구의 포인터 | Project 4의 필드 계약 |
| [채택 결정](../decisions/FRAME_ANIMATION_V1_ADOPTION_20260930.md) | 사용자 박준태의 옵션 A와 OAuth·token·cancel·retry 경계 | 독립 감사 PASS, 병합 완료, credential 발급 |
| [상세 설계](../FRAME_ANIMATION_V1_DESIGN_KO.md) | 3~13절, 10.5, 11.5, 16~17절의 설계 근거 | 설치 증거 |
| [실행·저장 설계](../FRAME_ANIMATION_V1_EXECUTION_STORAGE_KO.md) | 1~11절, 특히 3.1, 5.4, 7.2.1, 8.2.1, 9.1 | 성능 수치, 서비스 자격 |
| [개발 안내](../FRAME_ANIMATION_V1_DEVELOPMENT_KO.md) | ANIM-001~023의 범위·선행·완료 증거 | dispatch 상태, host record |
| 고도화 설계의 pin된 1~5절 | transfer edge, pack, 완료 facet | 중앙 구현, host qualification |
| 이 ADR과 스키마 명세 | 이후 구현 태스크가 읽는 정본 | 코드, 유료 실행, 작품 승인 |

## 2. 이미 채택한 방향

채택 결정이 이미 고른 방향이라 이 ADR이 다시 묻지 않는다.

- 새 모드에서만, 고정 FramePlan의 remote compose/encode를 ExecutionPlan·사용권·예산 안에서 실행한다. creative generation은 기존 A/B/C quote·승인 경로다. compile이 생성을 임의 호출하지 않는다.
- `LOCAL_FULL`은 독립 복사와 offline replay를 유지한다. `DRIVE_BOUNDED`는 고정 object/member·revision·hash로 online replay와 명시적 offline restore를 제공하고, 삭제·권한 회수·접근 불가를 재현 불가로 보고한다.
- 프로젝트 상태, 선택, receipt, coverage, build id, seal 쓰기는 coordinator 하나다. 바뀌지 않는 snapshot의 독립 계산은 병렬일 수 있다.
- 브라우저 Google OAuth, Drive archive, qualification된 worker, 복수 encoder를 새 모드의 개발 범위로 둔다.
- 저장 위치, 실행 위치, encoder는 따로 고른다. 성능 비교는 같은 품질의 전체 완료 시간, PC/worker peak 공간, 전송, 사용량이다. GPU 광고 fps만으로 고르지 않는다.

### 2.1. 이 ADR이 확정하는 schema fixation

설계가 제안하고, 아래 현행 소비자를 확인한 뒤 번호와 인코딩을 고정한다. 새 작품 방향이 아니다.

- Project 4, Shot 3, Build 2. Lyrics 1과 Audio 1 선언은 유지한다. 분석 파일의 `"0.1"` 의미도 유지한다.
- Animation timeline, asset registry, shot plan, animation review, StorageArchive, ExecutionPlan, CapabilityEvidence, EncodeRecipe, WorkerProtocol, FrameStream, pack index, seal, render manifest는 각 schema 1이다.
- pack magic `FAV1PACK`, header 16바이트, little-endian version 1. CANON_JSON_V1. 새 문서의 `document_type`.
- hard cut은 `type: HARD_CUT`, `overlap_frames: 0`, curve 없음이다. `CROSSFADE`는 overlap이 1 이상이고 `curve: LINEAR_INTERIOR_V1`만 허용한다. 마지막 entry의 `transition_out`은 null이다.

### 2.2. 이 ADR이 결정하지 않는 것

필요하면 구현을 멈추고 `DECISION_REQUIRED`로 올린다.

- `DIRECT_DRIVE`, worker의 Drive 직접 인증, desktop inbound listener, NAT port forwarding, 새 relay 배포
- OAuth Cloud project 생성, credential 발급, client secret을 비밀로 신뢰하는 설계
- 기존 프로젝트의 자동 모드 전환, legacy review/LOCK의 승계
- 무가사·instrumental Final 면제
- legacy Preview/Final 의미, Build 1 replay, `validate_manifest`의 overlap 거부를 새 모드에 맞추어 완화
- 품질 허용 숫자의 발명. 비교 축과 “driver마다 느슨하게 하지 않는다”만 고정하고, 숫자는 ANIM-015가 모든 driver에 같은 기준으로 정한다
- 실제 W00 컷 ID, 1920×1080을 전 프로젝트 기본값으로 바꾸는 일
- 중앙 `#47` source `09e161caa652d75e9617caf632b3b9899be35740`를 운영 증거로 쓰는 일
- 유료 생성, 추가 과금, 실제 작품의 최종 출력 승인, control-plane runtime 활성화, 공개 release

## 3. 모드와 버전 소비자

plan commit의 현재 reader는 다음만 안다.

| 데이터 | 현재 값 | 현재 소비자 | 새 모드 |
|---|---|---|---|
| Project | 3 | `engine/schema.py`의 `PROJECT_SCHEMA`, `init_project`, `migrate_project`, `_check_version` | 4는 별도 reader. legacy reader는 4 이상을 계속 거부한다 |
| Shot | 2 | `engine/production.py`가 `SHOT_SCHEMA`를 기록. `validate_manifest`는 overlap을 거부한다 | Shot 3과 frame edit validator. legacy 샷 레코드를 다시 쓰지 않는다 |
| Lyrics | 1 | `engine/lyrics.py` | 1 유지. cue를 컷 편집으로 옮기지 않는다 |
| Audio 선언 | 1 | `SCHEMA_VERSIONS` | 1 유지 |
| Audio 분석 파일 | `"0.1"` | `engine/audio.py` | 필드 의미를 유지한다 |
| Build | 1 | `engine/builds.py` | Build 2는 새 문서. Build 1 concat replay는 남긴다 |
| visual/lyric review | 2 | `engine/resolver.py`, `engine/qc.py`, `engine/lyrics.py` | `animation_review` 1로 복사하지 않는다 |
| timeline edit log | 1 | `manifest/timeline_edits.json` | `animation_timeline`과 다른 문서다 |
| take registry | 1 | `manifest/assets.json` | `animation_asset_registry`와 다른 문서다 |
| packet | 1 | `engine/packets.py` | ExecutionPlan과 다른 문서다 |

`_check_version`은 project schema `0.1`, `0.2`, `1`, `2`, `3`만 받고 bool과 그 밖의 값을 거부한다. 키별 `schema_versions`는 1 이상, 선언 버전 이하의 정수만 받는다. 미래 버전을 조용히 받아들이지 않는다. 새 reader도 bool을 정수로 받지 않는다.

`production_profile`이 없거나 `LEGACY_MV`이면 현재 경로다. `FRAME_ANIMATION_V1`은 명시적 전환으로 만든 Project 4에만 붙인다. 프로젝트를 열거나 기존 `migrate_project()`를 실행한 것만으로 프로필, 타임라인, 자산, 승인을 만들지 않는다.

### 3.1. migration

ANIM-002가 구현할 전환은 다음만 한다.

1. 명시적 사용자 동작으로만 시작한다.
2. 쓰기 전에 `project.yaml`과 관련 manifest를 `migrations/`에 배타적으로 백업한다. 백업 digest가 다르면 중단한다.
3. `input/master.mp3` 또는 `master.wav`, `input/lyrics.txt`, 검토된 cue, 기존 `builds/`, LOCK, 유료 take의 바이트를 바꾸지 않는다.
4. legacy ms 경계를 `frame_at(ms, fps) = (ms * fps + 500) // 1000`으로 한 번 매핑하고, 차이와 1프레임 미만을 보고한다. 실패를 조용히 잘라 맞추지 않는다.
5. 첫 타임라인은 overlap 0인 `HARD_CUT`만 만든다. twos, crossfade, `motion_intent`, A/B/C 경로를 추정하지 않는다. 추정 전 `motion_intent`는 비어 있으며 Final을 통과하지 못한다.
6. legacy review 2와 legacy LOCK을 animation review나 animation lock으로 복사하지 않는다.
7. 전환 뒤에도 프로젝트 안의 Build 1 디렉터리는 Build 1 replay로 연다.

## 4. 읽기·쓰기 정본

| 내용 | 정본 | 정본이 아닌 것 |
|---|---|---|
| 원곡 바이트와 음악 시간 | `input/master.mp3` 또는 `master.wav`와, 다를 때만 따로 승인된 사용 범위 | 컨테이너 표시 길이, AAC priming/padding |
| 가사 원문 | `input/lyrics.txt` | 타이밍 문서, 음성 인식 |
| 가사 cue | `lyrics/lyrics_timed.json`의 검토된 cue | 컷 경계 |
| 편집 순서, 사용 구간, 전환 | `timeline/edit.json` | `manifest/shots.json`의 ms, `timeline/derived.json`, CSV |
| 연출과 MotionPlan | `manifest/shots.json`과 `animation/shots/<id>/plan.json` | `render_mode` 하나 |
| 자산 바이트 | revision과 SHA-256 | filename, mtime, Drive file ID |
| 검수·LOCK | `production/approvals.jsonl`, `manifest/animation_locks.json` | 파일명 `final`, sealed build를 고친 결과 |
| 프레임 시각 | 0-based 정수 프레임, 끝 제외 구간 | encoder PTS. PTS는 프레임 시계에 대한 검사다 |
| 빌드 | Build 2 inventory 또는 pin된 archive manifest | 라이브 프로젝트, 최신 file ID |

ms 파생 view는 프레임 정본의 함수일 수만 있다. 반올림한 ms를 다시 프레임 정본에 쓰지 않는다.

## 5. 시간축과 전환

내부 인덱스는 0부터다. 표시 번호는 `frame_index + 1`이고 파일은 `F_` 뒤 6자리다. 24fps에서 내부 프레임 `f`의 노출은 `f/24`초에 시작해 `(f+1)/24`초에 끝난다. ones/twos와 소스 FPS는 출력 FPS를 바꾸지 않는다.

`LINEAR_INTERIOR_V1`에서 overlap `O`의 0-based 위치 `k`는 incoming `(k+1)/(O+1)`, outgoing `1 - incoming`이다. `O = 1`이면 각 `1/2`이다. 색 공간과 alpha는 recipe에 고정한다. FFmpeg xfade와 같다고 가정하지 않는다. xfade를 쓰려면 같은 프레임 수, weight, 끝점을 fixture로 검사한다.

한 전역 프레임의 완성 이미지는 하나다. 기여하는 컷 원본은 하나 또는 둘이다. 세 컷이 겹치면 거부한다.

### 5.1. 180프레임과 출력 90번

같은 인덱스 규칙이다.

- I001 사용 `[0, 96)`, I002 사용 `[0, 96)`, overlap 12.
- `S0 = 0`, `E0 = 96`, `S1 = 84`, `E1 = 180`.
- `96 + 96 - 12 = 180`.
- I001 출력 `[0, 96)`, I002 출력 `[84, 180)`, 전환 `[84, 96)`.
- 표시 번호 1–96, 85–180, 전환 표시 85–96.

출력 파일 90번은 표시 번호 90, 내부 `frame_index` 89, `final_frames/F_000090.png`이다.

- `0 <= 89 < 96`이므로 S001 local 89.
- `84 <= 89 < 180`이므로 S002 local `89 - 84 = 5`.
- `k = 5`, `O = 12`. S002 weight `6/13`, S001 weight `7/13`. 스키마의 `[7, 13]`과 `[6, 13]`이 이 값이다.

I001의 뒤 handle 12는 사용 구간에 들어 있지 않다. 원본은 최소 108프레임이어야 하며, 108번째 이후의 미사용 프레임은 180에 더하지 않는다.

### 5.2. 240초·5760프레임

`240 * 24 = 5760`. 내부 5759의 노출은 `5759/24`초에 시작해 240초에 끝난다. 마지막 프레임의 파일은 `F_005760.png`이다.

예시 S240-B는 실패다. 60개 entry의 사용 길이가 모두 96이면 합은 5760이다. overlap 12가 12개이면 합은 144이고 출력은 `5760 - 144 = 5616`, 234초다. 목표 5760과 다르므로 검증 오류다. 미사용 handle은 사용 길이를 늘리지 못한다. 사용 구간이나 overlap을 다시 계획한다. 5616에 맞춰 목표를 덮어쓰지 않고, 5760에 맞춰 overlap을 조용히 지우지 않는다.

예시 S240-A는 같은 식을 만족한다.

- entry 0..47의 사용 길이는 96. entry 0..46의 overlap은 0, entry 47의 overlap은 12.
- entry 48..59의 사용 길이는 108. entry 48..58의 overlap은 12. entry 59는 null.
- 사용 합 `48*96 + 12*108 = 5904`.
- overlap 합 `12*12 = 144`.
- `5904 - 144 = 5760`.

중간 컷은 `0+12 <= 96`, `12+12 <= 108`이다. 인접하지 않은 세 컷이 한 프레임을 공유하지 않는다. 240초 프로필에서는 timeline `target_frames`와 project `output_frames`가 둘 다 5760이어야 한다.

240초 음원은 240초 제작 마스터이거나, 원본을 덮어쓰지 않고 따로 승인한 240초 사용 범위다. 자동으로 자르거나, 늘리거나, 무음을 붙이지 않는다. AAC metadata 때문에 5760프레임 조건을 낮추지 않는다. 컷 편집만으로 cue를 옮기지 않는다.

## 6. 샷, 자산, 노출, RGBA

동작 의도 `STATIC` / `ANIMATED`, 구간 path `A` / `B` / `C`, 결과 종류 `FRAME_SEQUENCE` / `COMPOSITE_SEQUENCE` / `VIDEO_CLIP`을 분리한다. `STATIC`은 사람이 고른 값이다. 움직이는 샷을 정지 그림으로 자동 대체하지 않는다.

자산은 id, revision, kind, provenance, 파일 hash, coordinate space, dependency, preparation, acceptance를 가진다. 내용이 바뀌면 새 revision이다. RGBA·mask·crop origin·pivot은 전용 import에서만 보존한다. 기존 RGB 참조 경로로 투명 레이어를 넣지 않는다.

ExposureSchedule은 트랙의 모든 출력 슬롯을 빈틈없이 덮는다. 빈 슬롯을 직전 그림으로 채워 Final로 올리지 않는다. 홀수 끝과 공유 anchor 중복을 구분한다. 컷을 넘는 동작 보간을 거부한다. 부족한 반환 길이를 속도 변경이나 끝 프레임 복제로 맞추지 않는다. provider가 끝점, pose, mask, 필수 참조를 지원하지 않으면 그 조건을 삭제하지 않고 `CAPABILITY_UNAVAILABLE` 또는 `NEEDS_MANUAL_WORK`로 멈춘다.

로컬 제작도 사람 검수가 필요하다. `generated=False`의 기술 `PASS_LOCAL`은 동작 승인이 아니다. 파일명만으로 포즈 시점을 승인하지 않는다. import는 draft다.

W00은 실제 본편 컷의 제작 순서이며 고정 pilot이나 합성 fixture로 대신하지 않는다. 채택 뒤에 `KEEP` / `CHANGE` / `MIX`가 있기 전에는 다음 production wave를 열지 않는다. 실제 작품 입력이 없으면 `PENDING`이다. 경로 변경 중 `SUBMITTING` / `UNKNOWN` 유료 작업을 버리고 다시 제출하지 않는다.

## 7. 검수와 LOCK

`PLAN_LOCK`, `WAVE_LOCK`, `FINAL_LOCK`은 서로 다르고, 비용 승인과 실제 출력 승인도 각각 다르다. content digest와 승인·상태 digest를 섞지 않는다. 승인 레코드가 자기 digest를 입력으로 요구하지 않는다.

컷 검수는 시퀀스 content digest, 사용 구간, MotionPlan, 자산 revision, 공통 표현 의도에 묶인다. 전환 검수는 양쪽 시퀀스, 출력 연결, recipe에 묶인다. 최종 승인은 실제 MP4, 최종 시퀀스, build inventory, 음원, 가사, font, 편집 digest에 묶인다. reviewer는 박준태 또는 명시적 위임자이며, 기록된 method는 그 사람이 실제로 본 재생과 검사다. 시스템이 의미 QC를 통과시켰다고 쓰지 않는다.

compile은 새 모드에서 `FINAL_CANDIDATE_READY`까지다. `FINAL_APPROVED`는 sealed build를 가리키는 별도 기록이다. 수정 뒤에는 새 파일을 다시 보고 승인한다. 완료 빌드는 보존한다.

무가사 Final 면제는 현재 제품에 없으며 이 계약이 추가하지 않는다.

## 8. 빌드와 replay

Build 2는 선택 원본, MotionPlan, recipe, 전환, frame_map, 자막, font, 음원, 최종 PNG 시퀀스, 출력을 보존한다. `LOCAL_FULL`은 폴더와 offline replay다. `DRIVE_BOUNDED`는 앱이 덮어쓰지 않는 object와 archive manifest이며, online replay와 명시적 restore를 제공한다. 라이브 파일이나 최신 ID로 snapshot을 대신하지 않는다.

PNG 정본은 인코드 전 결과다. MP4와 PNG의 픽셀이 같다고 보장하지 않는다. 같은 시각 버전과 시간축을 쓴다. 디코드 검사는 프레임 수, `24/1`, rational PTS, 첫 시점 0, 마지막 노출 종료, 원곡 트랙이다.

한 컷의 교체는 그 closure만 다시 계산한다. 다른 source/recipe, 원곡 hash, 가사 원문과 허용하지 않은 cue는 유지한다. font나 cue는 subbed와 전달 승인만 stale로 만든다. encoder 변경은 PNG를 재사용할 수 있으나 새 MP4와 최종 파일 승인이 필요하다. locator만 바뀌고 바이트가 같으면 compose를 재사용한다. cache hit가 승인을 만들지는 않는다.

변경 범위의 소비자:

| 이후 태스크 | 이 계약에서 읽는 것 | 자동으로 넘어가지 않는 것 |
|---|---|---|
| ANIM-002 | 3.1의 migration | 승인 승계, 모드 자동 전환 |
| ANIM-003~004 | 자산 hash, RGBA, exposure coverage | 빈 슬롯 자동 hold |
| ANIM-005 | 5.1의 180 규칙과 frame_map weight | 세 컷 겹침 |
| ANIM-006~007 | 검수 binding, PLAN/WAVE/FINAL, W00 | 출력 승인, 합성 fixture를 W00으로 기록 |
| ANIM-013 | OAuth, archive, pack, restore | worker Drive 인증 |
| ANIM-014 | FrameStream, job 상태, journal | UNKNOWN 해제용 재제출 |
| ANIM-015 | driver 분리, 같은 DeliveryProfile | FFmpeg flag를 native 완료로 기록 |
| ANIM-016 | 사용권·probe·수동/자동 구분 | 구독을 API 과금이나 encode 자격으로 추론 |
| ANIM-017 | 부분 재계산, edge 비용 | 측정 없는 가속 수치 |
| ANIM-018 | 실제 한 경로의 240초 증거 | code/fake PASS, 사용자 승인 전 merge |
| ANIM-019 | CapabilityEvidence의 현재 scope | 환경이 없을 때의 `NOT_REQUIRED` |
| ANIM-020 | 8절의 digest와 무효화 | self digest, clean/subbed 병합 |
| ANIM-021 | journal, upload checkpoint, seal | 중앙 감사 재개를 제품 retry로 사용 |
| ANIM-022 | W00과 전체 재생 기록 | 합성 검사로 사람 승인 생성 |
| ANIM-023 | 001~022의 독립 facet | 018 baseline만으로 분모 23을 닫기 |

기존 18개 node의 완료 기록에 019~023의 수용 범위를 소급하지 않는다. 개발 분모 23은 이 plan revision의 작업 수이며 자격·작품 승인·release의 합이 아니다.

## 9. 저장, OAuth, token, relay

Google OAuth client와 Cloud project는 배포 책임자의 등록 앱이다. installed-app client ID는 공개 설정이다. client secret을 비밀 저장소나 인증 수단으로 신뢰하지 않는다. system browser, PKCE, state, 공식 redirect를 검사한다. 이 계약은 Cloud project 생성이나 credential 발급을 실행하지 않는다.

토큰은 OS credential store에만 둔다. store를 쓸 수 없으면 연결을 막거나, 사용자에게 밝힌 메모리 한정 세션으로만 연결한다. mode 0600 settings 파일로 OAuth token을 자동 저장하지 않는다. 로그아웃, 권한 회수, 계정 전환은 token과 job 연결을 버리고 이후 접근을 막는다. 프로젝트, 빌드, packet, 로그, Git에 비밀번호, refresh token, authorization code, cookie, bearer, upload URI 원문을 넣지 않는다.

기본 scope는 앱이 만들었거나 사용자가 선택한 파일의 `drive.file`이다. 폴더 선택이 기존 하위 파일 전부를 허용한다고 가정하지 않는다.

기본 데이터 경로는 `COORDINATOR_RELAY`다. Drive 또는 local archive에서 coordinator로, worker로, 다시 coordinator로, archive로 간다. 사용자 OAuth와 refresh token은 coordinator 밖에 나가지 않는다. `MANUAL_PACKET`은 probe된 크기와 사람 동작, 반환 형식 안에서만 따로 표시한다. 두 경로를 하나의 “Drive 직접 처리”로 부르지 않는다.

coordinator 기본 위치는 `USER_DESKTOP`이다. PC 디스크를 적게 써도 WAN 바이트는 PC를 지난다. v1 연결은 고정 allowlist의 worker HTTPS endpoint로 coordinator가 outbound TLS를 연다. 검증된 입력을 bounded push하고, 확인된 결과 locator에서 bounded pull한다. inbound listener와 cross-host redirect는 기본 경로가 아니다.

worker peer credential과 Drive OAuth credential은 다르다. 실행 중 새 credential이나 service account를 발급하지 않는다. 없으면 그 route는 `UNQUALIFIED`다. grant는 issuer, audience, peer, `credential_epoch`, job, attempt, snapshot, object, range, bytes, 만료, nonce를 모두 확인한다. 범위 밖 browse, submit, URL fetch를 거부한다. 만료 grant의 갱신은 새 compute submit이 아니다.

`credential_epoch`는 계정 전환, 로그아웃, 권한 회수 때 올라간다. 그 뒤의 새 read, write, submit, upload resume은 거부한다. 이미 제출된 job의 기록은 지우지 않는다. token 폐기만으로 `CANCEL_CONFIRMED`를 만들지 않으며 UNKNOWN과 지출 예약은 유지한다.

PC, worker의 disk, RAM, VRAM을 각각 예약한다. 단계는 `READ → VERIFY → DECODE → COMPOSE → ENCODE/ARCHIVE → VERIFY → EVICT`이다. 느린 소비자는 생산자를 멈춘다. spool을 무한히 늘리지 않는다. 최소 묶음이 cap보다 크고 나눌 수 없으면 필요한 용량을 보이고 중단한다.

Workspace 예시의 2 GiB와 8 GiB는 fixture다. 장당 3 MB 가정은 용량 계획의 예시이며, 최종 시퀀스만 약 17.28 GB라는 계산도 입력 PNG만의 예시다. 구현 전 완료 시간을 약속하지 않는다.

## 10. 실행, encoder, cancel, retry

ExecutionPlan이 없는 작업을 실행하지 않는다. `capability_evidence_required`가 참이면 현재 scope의 증거 없이 `AUTO_PERFORMANCE` 후보가 되지 못한다. `allow_additional_charges: false`인 동안 추가 과금 경로로 우회하지 않는다.

FrameStream의 시간 정본은 프레임 인덱스와 rational PTS다. encoder 입력 기본값에 의존하지 않는다. FFmpeg image2를 쓸 때도 framerate 24와 start number 1을 명시한다. 다른 backend도 같은 FPS, PTS, 순서를 지킨다.

encoder driver와 codec을 구분한다. `FFMPEG` 안의 hardware flag는 `NVIDIA_NATIVE`, `VIDEOTOOLBOX_NATIVE`, `GSTREAMER`, `QUALIFIED_SERVICE`의 완료가 아니다. native encode가 실제로 같은 DeliveryProfile의 프레임, PTS, 원곡을 만들기 전에는 복수 driver 완료로 세지 않는다. MediaVerifier는 worker와 독립이다. worker나 LLM의 완료 문장을 검증으로 승격하지 않는다.

job 상태와 fence는 스키마 13절이다. 이 ADR이 추가로 고정하는 운영 의미는 다음과 같다.

- `CANCEL_REQUESTED`와 `CANCEL_CONFIRMED`를 구분한다.
- 완료와 취소가 경합하면, 완료가 먼저 확정된 결과를 `OUTPUT_PENDING_VERIFY`로 검사한다.
- `RUNNING` 또는 `OUTPUT_PENDING_VERIFY`에서 종료를 모르면 `UNKNOWN`이다.
- UNKNOWN 동안 예약 해제, 대체 제출, 새 attempt를 거부한다.
- 취소 자체를 provider 환불로 적지 않는다.
- `FAILED_CONFIRMED` 뒤의 새 attempt는 사용자의 명시적 이어하기와 남은 bounded allowance, quote, 사용권, 예산 재확인이 있을 때만 시작한다. 자동 재제출은 없다.
- 같은 attempt의 HTTP retry는 스키마의 idempotent GET/Range와 같은 session status query뿐이다. 미설정 retry는 0이다. create, compute submit, manifest 게시의 불명을 retry로 풀지 않는다.

paid quote, 예약, UNKNOWN fence, 중복 제출 금지는 기존 생성 계약과 같다. 첫 side effect 전에 `SUBMIT_INTENT`를 durable journal에 남긴다. 접수 응답이 없어도 job key와 attempt를 바꾸지 않는다.

seal은 required artifact마다 최소 `UPLOAD_HASH_MATCHED`가 있어야 한다. provider가 신뢰 가능한 SHA-256과 length를 주지 않으면 `FULL_READBACK`이 필요하다. `UPLOADED_UNVERIFIED`만으로 checkpoint나 완료 seal을 쓰지 않는다. profile이 readback을 요구하면 checksum으로 면제하지 않는다. member 검증과 full-pack 검증을 서로 승격하지 않는다.

완료 manifest 전에는 필요한 원본, 전달 PNG 시퀀스, clean 재현 recipe, frame_map, 원곡, cue/font, clean MP4, subbed MP4가 보관·검증되어야 한다. `ENCODED`, `RENDERED`, `UPLOADED`는 완료가 아니다. 게시 불명은 `SEAL_UNKNOWN`이며 같은 identity로만 확인하고 새 COMPLETE를 만들지 않는다.

## 11. 완료 facet

pin된 고도화 설계 5절의 네 facet를 그대로 쓴다. 한 상태의 완료로 개발 병합, runtime 자격, 작품 검수, 공개 배포를 동시에 표시하지 않는다.

| facet | 이 태스크에서 보고하는 값 | 근거 |
|---|---|---|
| `node_state` | 작성자가 `DONE`을 기록하지 않는다 | `DONE`은 host-pinned delivery의 실제 merge 뒤 중앙 기록이다. PR을 연 사실은 `DONE`이 아니다 |
| `qualification_state` | 제품 runtime은 `UNQUALIFIED` | Drive, worker, native encoder, 구독 probe가 없다. 문서 계약이 그 환경을 요구하지 않으므로 환경 부재를 `NOT_REQUIRED`로 면제하지 않는다 |
| `acceptance_state` | 작품 수용은 `PENDING` | 사람 검수와 실제 출력 승인이 없다. 엔지니어링 리뷰를 작품 `ACCEPTED`로 적지 않는다 |
| `release_state` | `NOT_AUTHORIZED` | merge나 이 문서가 배포 권한을 만들지 않는다 |

중앙이 facet를 구현하기 전에는 host record를 만들지 않는다. 위 표는 draft다. fake 성공, 문서 작성, 개발 merge를 `QUALIFIED`, `ACCEPTED`, `RELEASED`로 올리지 않는다.

ANIM-018은 `user_merge: true`, `astra_auto_merge: false`인 실제 자격 milestone이다. 허용된 한 경로의 240초·24fps·1080p 입력, relay/worker, 검증된 PNG/MP4, archive, replay/restore, UI, 실패와 verification 증거, 그리고 그 exact evidence에 대한 사용자 승인 전에는 merge와 `DONE`을 하지 않는다. 없으면 `WAITING`이다. 013~017의 code-only 개발 병합은 018의 자격을 대신하지 않는다. 023은 018이 빠져 있으면 전체 개발 closeout도 `WAITING`이며, 분모에서 018을 빼지 않는다.

이 명세를 plan commit에 고정한 사실은 중앙 구현, host qualification, 제품 사용 가능, 작품 승인, release의 증거가 아니다.

## 12. 차단 수용 조건

이후 구현이 만족해야 하는 조건이다. 이 문서 작성에서 그 테스트를 실행한 것은 아니다.

| ID | 차단 |
|---|---|
| `BLOCK-EMPTY-EXPOSURE` | 노출 슬롯이 비었거나, 빈 슬롯을 암시적 직전 그림으로 채워 Final로 올리면 거부한다 |
| `BLOCK-SHORT-SOURCE` | 사용 구간이나 handle이 원본 인덱스 밖이거나, 반환 시퀀스가 소유 구간보다 짧으면 거부한다. 승인 없는 속도 변경과 끝 프레임 채움을 하지 않는다 |
| `BLOCK-UNREVIEWED` | content digest에 묶인 현재 사람 검수가 없는 시퀀스는 Final과 current approval을 통과하지 못한다. 로컬 제작과 `PASS_LOCAL`도 같다 |
| `BLOCK-STALE-APPROVAL` | 대상 digest와 다른 승인은 stale이며 Final과 범위 밖 production을 승인하지 못한다. cache hit로 되살리지 않는다 |
| `BLOCK-TRIPLE-OVERLAP` | 한 프레임을 세 entry 이상이 덮으면 거부한다. 다음 entry가 있는데 `O`가 `0 <= O < min(L_i, L_(i+1))` 밖이거나, 중간 entry에서 `O_(i-1)+O_i > L_i`이면 거부한다 |
| `BLOCK-TARGET-MISMATCH` | `target_frames`, project `output_frames`, `sum(L)-sum(O)`가 다르면 거부한다. S240-B는 pairwise overlap이 유효해도 5616이라 거부한다. 어느 쪽 숫자도 자동으로 덮어쓰지 않는다 |

추가 차단: 필수 제어 삭제, quote·입력·권한 binding 변경 후의 제출, UNKNOWN 중 재제출, token이 packet에 있음, `DIRECT_DRIVE`를 후보에 넣음, 최소 verification level 전의 seal, 부분 member 검증을 full-pack 검증으로 기록, self digest, 미래 schema의 silent accept.

### 12.1. legacy 보존

| ID | 유지 |
|---|---|
| `KEEP-LEGACY-PROFILE` | 프로필이 없거나 `LEGACY_MV`이면 현재 ms 타임라인, `validate_manifest`의 overlap 거부, 현재 reader가 유지된다 |
| `KEEP-PREVIEW-FINAL` | Preview는 구조가 유효하면 부족한 창작 자산을 placeholder까지 fallback한다. 잘못된 타임라인, 마스터 손상, FFmpeg 부재는 숨기지 않는다. Final은 현재 LOCK, 검토된 가사, font, 샷마다 자격 있는 자산을 요구한다. 화질 선택이나 파일명 `final`은 승인이 아니다 |
| `KEEP-BUILD1-REPLAY` | Build 1은 캡처한 clip, 원곡, ASS의 concat replay이며 라이브 프로젝트 없이 inventory 변조를 탐지한다 |
| `KEEP-NO-AUTO-MODE` | 열기나 기존 metadata migration만으로 `FRAME_ANIMATION_V1`이 되지 않는다 |
| `KEEP-NO-APPROVAL-INHERIT` | 기존 검수와 LOCK이 새 계약의 현재 승인이 되지 않는다 |
| `KEEP-AUDIO-LYRICS` | 컷과 전환 변경이 원곡 파일, 가사 원문, 검토된 cue를 옮기거나 다시 쓰지 않는다 |

회귀로 유지할 현재 검사:

- `tests/test_compiler_v03.py`의 4분 Preview, Final 게이트, replay/tamper
- `tests/test_timeline.py`의 컷 편집이 가사와 무관한 샷 승인을 유지하는 검사
- `tests/test_schema_presets.py`의 migration 보존과 미래 버전 거부

이 계약의 새 테스트를 기존 완료 기록에 소급하지 않는다. CI의 합성 4분 fixture는 W00이나 최종 작품 검토가 아니다.

## 13. 감사 경계

이 계약은 아키텍처와 승인 의미를 정하므로 A3이며 non-author 검토가 필요하다. 사용자 결정의 근거는 채택 결정 문서와 승인 포인터 #58이다. 그 문서는 독립 감사 PASS를 대신하지 않는다고 적혀 있다.

이 ADR을 작성한 세션의 자체 확인, pytest, 문서 대조는 독립 감사 PASS가 아니다. 구현 PR의 정확한 HEAD는 작성자가 아닌 reviewer가 본다. engineering 검토는 생성 비용 승인, 작품 최종 승인, release를 대신하지 않는다.

실제 서비스, GPU, 권한이 없는 동안 이후 태스크는 fake로 protocol만 검사하고 실제 qualification을 미완료로 남긴다. fake 성공을 실제 완료로 세지 않는다. 기능 없는 모듈 파일만 만드는 것을 완료로 세지 않는다. 이 ANIM-001 변경은 문서를 추가하고 engine 동작을 바꾸지 않는다.
