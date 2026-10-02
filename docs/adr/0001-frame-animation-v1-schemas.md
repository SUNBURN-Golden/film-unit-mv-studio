# FRAME_ANIMATION_V1 schema 명세

이 문서는 [ADR 0001](0001-frame-animation-v1-contract.md)과 하나의 계약이다. 구현은 여기의 `document_type`·`schema_version`·필수 필드를 읽고, ADR의 금지와 권한 문장을 함께 적용한다. 필드가 이 문서에 없으면 구현하지 않는다. 더 높은 버전을 조용히 받아들이지 않는다.

이 명세는 코드를 설치하지 않는다. 아래 JSON은 필드 계약의 예시이며, 작품 승인·자격·실행 기록이 아니다.

## 1. 공통 인코딩

### 1.1. 문서 정체

새 모드의 JSON 문서는 `document_type`과 `schema_version`을 둘 다 가진다. `schema_version`만으로 종류를 구분하지 않는다. 기존 제품에는 이미 서로 다른 `schema_version: 1` 문서가 있다.

| 기존 파일 | 기존 schema | 새 문서와 구분 |
|---|---|---|
| `analysis/audio.json` | `"0.1"` 문자열 | 음원 분석. 새 모드가 의미를 바꾸지 않는다 |
| `project.yaml`의 `schema_versions.audio` | 정수 1 | 선언 버전. 분석 파일의 `"0.1"`을 다시 쓰지 않는다 |
| `manifest/timeline_edits.json` | 1 | legacy 편집 이벤트 로그. `animation_timeline`이 아니다 |
| `manifest/assets.json` | 1 | legacy take registry. `animation_asset_registry`가 아니다 |
| `render/packets/packets.json` | 1 | legacy hand-off. ExecutionPlan이 아니다 |
| `builds/B####/build.json` | 1 | Build 1. Build 2가 아니다 |
| visual/lyric review | 2 | `animation_review`로 승계하지 않는다 |

`document_type`이 없는 기존 파일은 기존 reader만 읽는다. 새 reader가 그것을 애니메이션 문서로 해석하면 실패다.

### 1.2. CANON_JSON_V1

해시 입력은 다음 바이트열이다. 저장 파일은 그 바이트열 뒤에 LF 하나를 붙인 것과 정확히 같아야 한다. 다른 공백은 거부한다.

- UTF-8, BOM 없음.
- 허용 타입은 object, array, string, integer, boolean, null뿐이다. float·NaN·Infinity·binary를 거부한다.
- object 키는 UTF-8 바이트 순으로 정렬한다. 중복 키는 거부한다.
- integer는 십진수이며 불필요한 앞자리 0, 부호 `+`, 지수 표기를 쓰지 않는다. 범위는 `-2^63` 이상 `2^63-1` 이하다. boolean을 정수로 받아들이지 않는다.
- string은 JSON 필수 이스케이프(`"`, `\`, U+0000–U+001F)만 사용한다. 그 밖의 `\u` 이스케이프는 정규형에 넣지 않는다.
- 구분자 앞뒤의 공백은 없다.

비율은 JSON 분수가 아니다.

| 용도 | 형태 | 제약 |
|---|---|---|
| FPS, PTS, duration | `{"den": d, "num": n}` | 정수, `d > 0`, `gcd(|n|, d) = 1` |
| frame_map weight | `[n, d]` | 정수, `n >= 0`, `d > 0`, `gcd(n, d) = 1` |

끝 제외 정수 구간은 `[start, end)`이며 `start`·`end`는 0 이상의 정수이고 `end >= start`다. 바이트 범위도 같은 규칙이다.

해시 필드는 소문자 SHA-256 64자이다. 내용 해시에 절대 경로, storage locator, job 상태, 검수 표시, 문서 자기 digest, 승인 상태를 넣지 않는다.

### 1.3. 버전 거부

각 `document_type`은 이 문서에 적힌 `schema_version`만 읽는다. 없거나, 정수가 아니거나, 더 높거나, 더 낮으면 거부한다. 알 수 없는 `document_type`도 거부한다.

## 2. 프로젝트 프로필

`production_profile`이 없거나 `LEGACY_MV`이면 Project 3 reader가 읽는다. `FRAME_ANIMATION_V1`은 Project 4에서만 유효하다.

Project 4의 `schema_versions`는 다음으로 고정한다.

| 키 | 값 |
|---|---:|
| project | 4 |
| shot | 3 |
| lyrics | 1 |
| audio | 1 |
| build | 2 |

`animation` 객체:

| 필드 | 계약 |
|---|---|
| `schema_version` | 1 |
| `output_frames` | 양의 정수. 작품 프로필의 목표 프레임 수. 240초·24fps 프로필에서는 5760 |
| `timeline` | `timeline/edit.json` |
| `assets` | `manifest/animation_assets.json` |
| `roles` | `production/roles.json` |
| `schedule` | `production/schedule.json` |
| `storage_archive` | `manifest/storage_archive.json` |
| `execution_plan` | `execution/plan.json` |
| `initial_wave` | 문자열. 예: `W00` |
| `delivery_sequence` | `SUBBED` 또는 사용자가 고른 별도 DeliveryProfile 이름 |

`format.crf`는 FFmpeg 예시 값이다. 다른 driver의 EncodeRecipe에 같은 숫자를 복사하지 않는다. `format.fps`는 정수 24로 둘 수 있으나, 실행·인코드의 정본 비율은 `{"num": 24, "den": 1}`이다.

1920×1080·`16:9`는 이 작품 프로필의 제안값이다. 기존 프로젝트의 기본 4:3을 바꾸지 않는다.

## 3. animation_timeline 1

경로: `timeline/edit.json`. 편집 순서·사용 구간·전환의 정본이다.

| 필드 | 계약 |
|---|---|
| `document_type` | `animation_timeline` |
| `schema_version` | 1 |
| `target_frames` | Project `output_frames`와 같아야 한다. 다르면 오류이며 어느 쪽도 자동으로 덮어쓰지 않는다 |
| `entries` | 편집 순서. 각 항목은 아래 필드 |

entry:

| 필드 | 계약 |
|---|---|
| `instance_id` | 타임라인 안에서 유일 |
| `shot_id` | `S` + 3자리에서 5자리 숫자 |
| `sequence_revision` | 채택 시퀀스 revision |
| `used_source_range` | `[start, end)`. 실제 사용한 전환 구간을 포함한다 |
| `unused_handles` | `{"before": n, "after": n}`. 아직 쓰지 않은 여유분만. 길이 합계에 넣지 않는다 |
| `transition_out` | 마지막 entry는 null. 그 외는 transition 객체 |

transition:

| 필드 | 계약 |
|---|---|
| `id` | 유일 |
| `type` | overlap이 0이면 `HARD_CUT`만, overlap이 1 이상이면 `CROSSFADE`만 |
| `to_instance` | 다음 entry의 `instance_id`와 같다 |
| `overlap_frames` | 정수 `O` |
| `curve` | `CROSSFADE`는 `LINEAR_INTERIOR_V1`만. `HARD_CUT`에는 curve를 두지 않는다. 다른 curve 이름은 거부한다 |

`HARD_CUT`과 overlap 0을 두 가지 표현으로 쓰지 않는다. fade-in/out은 한 entry 안의 효과이며 overlap을 자동으로 빼지 않는다. 독립 길이를 가진 제목·검은 화면은 entry다. 자막 overlay는 entry 길이에 더하지 않는다.

길이:

```text
L_i = used_source_range.end - used_source_range.start
S_0 = 0
E_i = S_i + L_i
S_(i+1) = E_i - O_i
output_frames = sum(L_i) - sum(O_i)
```

`O_i`는 entry i의 outgoing overlap이다. 마지막 entry의 outgoing overlap은 0이다.

거부:

- 다음 entry가 있을 때 `O_i < 0` 또는 `O_i >= min(L_i, L_(i+1))`. 마지막 entry는 transition이 null이고 outgoing overlap은 0이다
- 중간 entry에 대해 `O_(i-1) + O_i > L_i`
- 어떤 전역 프레임이 세 entry 이상에 포함됨
- 사용 구간 또는 handle이 실제 원본 인덱스 밖에 있음
- `target_frames != output_frames`

`timeline/derived.json`은 `document_type: animation_timeline_derived`, `schema_version: 1`의 재계산 view다. 정본에 쓰지 않는다.

### 3.1. 180프레임 예시

`target_frames` 180. I001 사용 `[0, 96)`, handle after 12, crossfade 12프레임 `LINEAR_INTERIOR_V1`. I002 사용 `[0, 96)`, transition null.

출력 구간은 I001 `[0, 96)`, I002 `[84, 180)`, 겹침 `[84, 96)`이다. `96 + 96 - 12 = 180`.

내부 프레임 89는 표시 번호 90, 파일 `final_frames/F_000090.png`이다. S001 local 89, S002 local 5. 겹침 안 인덱스 `k = 5`, `O = 12`.

```text
incoming(S002) = (k + 1) / (O + 1) = 6/13
outgoing(S001) = 1 - incoming = 7/13
```

S001 원본은 사용 96프레임과 뒤 handle 12프레임을 합쳐 최소 108프레임이다. handle 12는 180 계산에 들어가지 않는다.

### 3.2. 240초 예시

24/1 fps에서 240초는 5760프레임이다. 내부 프레임 5759의 노출은 `5759/24`초에 시작해 240초에 끝난다.

예시 S240-B는 차단 예시다. entry 60개의 `L = 96`(합 5760)이고 outgoing overlap 12가 12개(합 144)이면 출력은 `5760 - 144 = 5616`프레임, 234초다. `target_frames` 5760과 다르므로 거부한다. 미사용 handle을 더해도 `L`은 늘지 않는다. 사용 구간이나 overlap을 다시 계획한다. 5616에 맞춰 `output_frames`만 덮어쓰지 않는다.

예시 S240-A는 같은 인덱스 규칙을 만족한다.

- entry 0..47: `L = 96`. entry 0..46은 `HARD_CUT` overlap 0, entry 47은 `CROSSFADE` overlap 12, `LINEAR_INTERIOR_V1`.
- entry 48..59: `L = 108`. entry 48..58은 같은 crossfade overlap 12, entry 59의 transition은 null.
- `sum(L) = 48*96 + 12*108 = 5904`
- `sum(O) = 12*12 = 144`
- `5904 - 144 = 5760`

중간 컷 검사 `12 + 12 <= 108`, `0 + 12 <= 96`을 만족하고, 한 프레임의 기여 컷은 둘 이하다.

## 4. animation_asset_registry 1

경로: `manifest/animation_assets.json`.

자산 공통 필드: `asset_id`, `revision`, `kind`, `provenance`, `files[]`, `content_sha256`, `coordinate_space`, `dependencies[]`, `preparation`, `acceptance`.

`files[]`의 각 항목은 `relative_name`, `sha256`, `byte_length`를 가진다. 참조는 `asset_id`만이 아니라 `revision`과 `content_sha256`을 고정한다. 내용이 바뀌면 새 revision이다. filename, mtime, Drive file ID만으로 일치라고 하지 않는다.

| kind | 추가 필수 |
|---|---|
| `MASTER_REFERENCE` | 시점, 고정 외형, 적용 범위 |
| `CLEAN_PLATE` | 카메라 범위, 보완 영역, 그림자 처리, provenance가 창작 보완이면 그 사실 |
| `LAYER_RGBA` | 원본 alpha, canvas, crop origin, pivot, z-order |
| `MASK` | 대상 asset revision, 범위, 채널 의미 |
| `REPLACEMENT_DRAWING` | 교체 대상, 시점, 호환 rig |
| `RIG_SPEC` | 부모, 자식, 기준점, 허용 변형, 가림 순서 |
| `CONTROL_IMAGE` | `keypose` / `breakdown` / `pose` / `layout`, 목표 시점 |
| `FRAME_SEQUENCE` | frame index, 파일 hash, exposure·composite recipe 참조 |
| `VIDEO_CLIP` | 원본 FPS, PTS, 크기, 사용 구간, 정규화 대응 |
| `COMPOSITE_SEQUENCE` | 합성 결과의 frame index, hash, recipe 참조 |

`preparation`에 `ready`만 적는 것은 준비가 아니다. 선택한 경로에 필요한 항목과 확인 근거를 적는다. 모든 자산에 모든 단계를 강제하지 않는다.

RGBA import는 기존 RGB 참조 import와 다른 경로다. 원본 alpha, mask, crop origin, pivot, coordinate space를 보존한다. 색 변환과 canvas 변경은 기록한다. 자동 crop으로 pivot을 옮기지 않는다.

## 5. animation_shot_plan 1

경로: `animation/shots/<shot_id>/plan.json`. Shot 3 레코드는 `manifest/shots.json`의 연출 필드와 이 plan을 함께 가리킨다. `in_ms` / `out_ms` / `duration_ms`는 읽기 전용 파생이며 편집 정본이 아니다. 라운드트립으로 `timeline/edit.json`에 쓰지 않는다.

필수:

- `shot_id`, plan `revision`, `motion_intent`(`STATIC` 또는 `ANIMATED`)
- 이야기·감정 역할
- 채택 자산의 id, revision, digest, layout 좌표
- 시작·종료 상태와 접촉·방향 전환·가림·재등장 사건 및 시점
- keypose·breakdown의 종류, 시점, 참조
- 프레임 구간별 `path` `A` / `B` / `C`와 필요한 capability
- 레이어·카메라별 exposure / transform schedule
- 준비 작업, 제작자, 검수자, 수정 범위

`motion_intent`, path, 결과 자산 kind를 `render_mode` 하나에 넣지 않는다. legacy `render_mode`는 LEGACY_MV 필드다.

제어 시점 예시는 키포즈 0·32·64·94, 브레이크다운 16·48·80이다. 이 7장은 완성 동작이 아니다. 캐릭터 twos와 카메라 ones가 같은 96프레임이면 캐릭터 노출 슬롯은 48, 카메라 상태는 96이다. 의도적 재사용으로 서로 다른 그림 수가 48보다 적을 수 있다. 모든 슬롯에 채택 그림이 있어야 하며, 빈 슬롯을 직전 그림으로 채워 Final로 올리지 않는다.

### 5.1. ExposureSchedule

트랙마다 `[start, end)`가 빈틈없이 대상 구간을 덮는다. 각 조각은 drawing 또는 state, duration, 의도적 hold, transform recipe를 가진다. twos의 시작 위상은 segment에 적는다. 홀수 끝의 한 프레임 노출도 적고, 공유 anchor의 중복과 구분한다.

구간 출력 소유는 `[start, end)`다. 공유 anchor는 다음 구간의 start에 한 번만 둔다. 도구가 양끝을 모두 반환하면 adapter가 중복을 명시적으로 제거한다. 마지막 anchor의 노출 길이는 따로 정한다. `2N-1`은 수량 식일 뿐 목표 길이·ones/twos의 결정 식이 아니다. 개수를 맞추려 속도 변경, 끝 hold, 무조건 복제를 하지 않는다. 컷 경계를 넘는 동작 보간은 거부한다.

## 6. SequenceArtifact

A/B/C의 공통 결과다. 필수: `state`(`draft` 또는 `accepted`), 컷 로컬 프레임 수, 파일별 hash, 원본/도구/설정, 시점 대응, dependency digest, preview 참조.

`accepted`는 사람 검수가 content digest에 연결되어 있을 때만 현재 승인이다. 로컬 합성과 영상을 다르게 면제하지 않는다. 원본은 보존하고, compiler 정규화 시퀀스는 파생이다.

경로별 최소 입력:

| path | 거부 조건 |
|---|---|
| A | 필수 마스터·layout·해당 시점 제어가 빠짐. 이전 프레임만 있는 체인을 유일한 기준으로 승인 |
| B | 계획상 end anchor가 있는데 start-only로 조건을 삭제. 반환 길이가 필요 길이보다 짧음. 승인 없는 속도 변경이나 마지막 프레임 채움 |
| C | 평면 회전으로 보이는 면의 변화를 대체. 전체 이미지 pan/zoom만으로 `ANIMATED` 완료를 기록 |

C의 v1 선언 범위는 RGBA 레이어, 부모·자식, pivot, translation/rotation/scale/opacity, mask, 명시적 z-order, replacement drawing, 카메라 transform, 그리고 구현 시 검증한 step/linear curve뿐이다. mesh, IK, 3D, 자동 립싱크는 미지원으로 적는다. 외부 시퀀스는 같은 SequenceArtifact로 받을 수 있다.

변형은 자산 coordinate space에서 계산한 뒤 컷 canvas에 합성한다. source alpha를 보존하고 필터는 premultiplied alpha를 쓴다. 제안 색 경로는 sRGB 원본을 선형 작업 공간에서 합성하고, 출력에 정한 sRGB/BT.709 변환을 기록하는 것이다. 실제 변환 이름은 recipe에 고정한다.

### 6.1. capability preflight

provider UI 선택과 작업 적합성을 분리한다. 필수 참조를 한도에 맞춰 자르지 않는다. 필수 제어가 없으면 `CAPABILITY_UNAVAILABLE` 또는 `NEEDS_MANUAL_WORK`로 멈추고, 그 조건을 버린 채 실행하지 않는다.

확인 항목: reference 포함 여부, pose/layout의 실제 입력 형식, mask/region, start/end와 끝점 포함 규칙, alpha 보존(모르면 미검증), native 크기·비율·FPS·길이·프레임 수, request/operation identity, 현재 quote·단위·상한·승인 범위.

## 7. 검수·LOCK

### 7.1. animation_locks 1

경로: `manifest/animation_locks.json`. legacy LOCK 파일을 이 스키마로 다시 쓰지 않는다.

| scope | 묶는 digest | 대신하지 않는 것 |
|---|---|---|
| `PLAN_LOCK` | 이야기, 음악, 컷 구성, 프레임 시간축, 화풍, 역할, 출력 규격 | 모든 컷의 완성 그림, 비용 승인 |
| `WAVE_LOCK` | 공통 표현 의도, 해당 컷의 계획·자산·사용 범위·capability | 다른 묶음의 상세 자산, 비용 승인 |
| `FINAL_LOCK` | 전체 편집 digest, 채택 컷, 전환, 색, 가사, font | 실제 출력 파일을 봤다는 사실 |

provider만 바뀌어도 실행 입력이 달라지면 quote와 승인을 다시 확인한다. 계획 수정은 `PLAN_LOCK`을 다시 요구하되, 바이트와 의존이 그대로인 컷 채택을 모두 지우지는 않는다. 기존 `WAVE_LOCK`은 그 범위 binding으로 다시 계산한다.

### 7.2. animation_review 1

경로에 추가하는 승인 기록: `production/approvals.jsonl`의 한 줄이 한 객체다. sealed build 파일을 고쳐서 승인을 넣지 않는다.

공통: `document_type` `animation_review`, `schema_version` 1, `scope`, 대상 digest, `reviewer`, `methods`, `decision`, `reviewed_at`.

| scope | binding |
|---|---|
| 컷 | sequence content digest, 사용 구간, MotionPlan digest, 자산 revision, 공통 표현 의도 |
| 전환 | 양쪽 채택 시퀀스, 출력 연결, transition recipe |
| `FINAL_FILM` | 실제 MP4 sha256, frame sequence root, build manifest sha256, edit digest, 음원·가사·font |

`FINAL_FILM` 예시는 설계 10.3의 필드를 따른다. `decision`이 `APPROVED`여도 reviewer가 박준태 또는 명시적 위임자이고, methods가 그 사람이 실제로 한 재생·검사를 적을 때만 현재 최종 승인이다. 파일명 `final`이나 이름만으로는 승인이 아니다. legacy review 2를 이 binding으로 복사하지 않는다.

표현 문제 처분: `FIX_REQUIRED`, `INTENTIONAL`, `ACCEPTED_LIMITATION`. 뒤의 둘은 이유와 범위가 필요하다. 규격 미충족, 손상, 프레임 누락은 표현상 수용으로 면제하지 않는다.

content digest에는 순서가 고정된 프레임 바이트, 시점, 노출, 합성 recipe만 들어간다. accepted 표시, 검수자, 승인 기록은 들어가지 않는다. 승인 객체의 입력에 자기 digest를 넣지 않는다.

## 8. 해시 층과 render manifest

`document_type` `render_manifest`, `schema_version` 1. ANIM-020의 소비자다. 픽셀 recipe와 승인 문서를 한 digest로 섞지 않는다.

| 이름 | 입력 | 제외 |
|---|---|---|
| `source_digest` | 역할, asset id, revision, 실제 바이트 sha256의 고정 순서 | locator, 승인 |
| `recipe_digest` | 시각 연산, 시간·색 정책, 필요한 toolchain | 절대 경로, job 상태, 검수 표시 |
| `clean_sequence_root` | 역할 `clean`, ordered frame index, PNG sha256, recipe 관계 | 승인, self digest |
| `subbed_sequence_root` | 역할 `subbed`와 같은 구조 | clean과 한 digest로 합치지 않음. 역할 문자열이 다르면 픽셀이 같아도 root는 다르다 |
| `encode_digest` | 인코드에 넣은 sequence root, DeliveryProfile, encoder/mux/검사 계약 | MP4 바이트 자체. MP4 sha256은 별도 artifact hash |

의존 순서는 asset bytes, exposure/motion/transform/mask, cut sequence, pairwise transition, clean output, subtitle overlay, subbed output, encode/mux artifact다.

무효화:

| 변경 | 다시 계산 | 검수 |
|---|---|---|
| locator만 이동, 바이트와 identity가 같음 | compose 재사용 | 기존 승인 유지 |
| 미채택 후보, 검수 메모, 준비 상태 | 픽셀 재계산 없음 | 채택 승인 유지 |
| font 또는 cue | subbed와 전달 출력 | clean과 컷 동작 검수는 유지, subbed·최종 파일 승인은 stale |
| 전환만 | 양쪽 입력·halo와 전환 출력 | 전체 편집·최종 출력 검토는 stale |
| encoder 또는 DeliveryProfile | encode/mux | PNG는 재사용 가능, 새 MP4와 최종 파일 승인 필요 |
| 공통 색 또는 렌더 도구 | 그 도구를 참조하는 closure | 관련 시각·기술 검토 |
| 채택 그림, keypose, rig, 사용 구간 | 설계 11.2의 해당 범위 | 영향 컷·전환·최종 파일 |

cache hit와 current approval은 따로 판정한다. 생성 모델의 재호출은 replay가 아니다. GPU·driver·native encode가 다른 바이트를 내면 새 artifact로 보관하고 독립 품질·PTS 검사를 한다. deterministic 순서가 모든 장치의 byte-identical 실행을 보장하지는 않는다.

## 9. frame_map과 Build 2

`builds/B####/frame_map.jsonl`의 각 줄은 전역 출력 프레임 하나다.

| 필드 | 계약 |
|---|---|
| `frame_index` | 0-based |
| `file` | `final_frames/F_` + 6자리 표시 번호 + `.png`. 표시 번호는 `frame_index + 1` |
| `output_sha256` | 그 PNG |
| `sources` | 하나 또는 둘. `instance_id`, `shot_id`, `local_frame_index`, `sequence_revision`, `weight` |
| `operations` | 전환·자막 등 recipe |
| `review_refs` | 외부 검수 id. content digest의 입력이 아니다 |

180 예시의 `frame_index` 89는 sources S001 local 89 weight `[7, 13]`, S002 local 5 weight `[6, 13]`, operation `CROSSFADE` / `LINEAR_INTERIOR_V1`이다. weight는 색 기여의 설명이며 픽셀 alpha 전체를 대체하지 않는다.

Build 2 `build.json`:

- `document_type`: `animation_build`
- `schema_version`: 2
- `storage_profile`: `LOCAL_FULL` 또는 `DRIVE_BOUNDED`
- 선택 원본, MotionPlan, exposure/composite recipe, 전환, frame_map, 자막, font, 음원, 최종 시퀀스, 출력의 inventory
- toolchain과 verifier 식별자

Build 1(`schema_version` 1, `document_type` 없음)은 기존 concat replay다. Build 2 replay는 보관한 최종 시퀀스와 음원의 재인코드, 그리고 선택 원본과 recipe의 재합성을 서로 다른 경로로 검증한다. 완료 빌드를 덮어쓰지 않는다. 다른 encoder·device·font의 새 인코드는 바이트 일치를 보장하지 않으며 새 artifact다.

출력 순서: 컷 노출·레이어·카메라, 전역 전환·색·제목, clean과 subtitle을 적용한 전달 시퀀스, `F_000001.png`부터 연속 PNG, 그 시퀀스로 `MASTER_SUBBED.mp4`, 디코드·inventory 검사. 최종 PNG를 손실 MP4에서 다시 뽑아 유일한 원본으로 삼지 않는다. 음원은 원곡에서 만든 AAC이며 생성 clip 오디오를 넣지 않는다. 자막은 컷 ms로 다시 계산하지 않고 원곡 cue를 전역 시간축에 적용한다.

기술 검사의 계약:

- PNG 개수·연속 논리 번호·hash. 로컬 폴더는 파일 검사, `DRIVE_BOUNDED`는 pack+index 검사와 번호 폴더 복원
- 디코드 프레임 수, FPS `24/1`, PTS와 duration의 rational timebase
- 첫 PTS 0, 마지막 노출 종료가 목표 길이와 일치
- 평균 FPS, container `nb_frames`, 파일명만으로 간격을 증명하지 않음
- 영상 트랙 하나, 지정 원곡 기반 음원 트랙
- alpha·색 공간·출력 색 metadata 정책이 recipe에 있음
- Preview의 임시 자료·미채택·미검수 동작이 전달 후보에 없음

Preview는 프레임별 불완전 상태를 표시할 수 있다. Final은 구조적 누락을 fallback하지 않는다.

## 10. StorageArchive 1과 pack

경로: `manifest/storage_archive.json`, `document_type` `storage_archive`, `schema_version` 1.

필수: `storage_profile`, object/member의 id·sha256·byte length·논리 번호, retention class, verification level, locator는 전송용으로만. 내용 identity는 hash다.

보존 계층: project revision, source objects, sequence packs, build objects, workspace, archive manifest. workspace는 다시 만들 수 있는 임시 자료다. 원본·채택 PNG·승인 빌드는 cache eviction 대상이 아니다.

### 10.1. pack bytes `FAV1` 

`pack_format_version` 1의 바이트 레이아웃:

| 오프셋 | 길이 | 값 |
|---:|---:|---|
| 0 | 8 | ASCII `FAV1PACK` |
| 8 | 4 | uint32 little-endian `1` |
| 12 | 4 | uint32 little-endian `header_byte_length` = 16 |
| 16 | 나머지 | index 순서의 PNG 원본 바이트. padding 없음. outer compression 없음 |

header에 pack hash나 index hash를 넣지 않는다. pack SHA-256의 입력은 header와 PNG body뿐이다.

이 레이아웃은 채택된 seekable pack 후보의 schema fixation이다. ZIP·TAR·중첩 압축은 이 실행 입력이 아니다.

### 10.2. pack_index 1

sidecar 객체. `document_type` `pack_index`, `schema_version` 1. index 파일은 16 MiB(16777216 bytes)를 넘기면 member decode 전에 거부한다.

| 필드 | 계약 |
|---|---|
| `pack_format_version` | 1 |
| `pack_sha256`, `pack_byte_length`, `header_byte_length` | 컨테이너 identity와 bounds |
| `snapshot_digest`, `recipe_digest`, `sequence_digest` | 승인 digest를 sequence digest에 섞지 않는다 |
| `members` | 고정 순서. member id, `frame_index` 또는 source 역할, `byte_offset`, `byte_length`, PNG sha256 |
| `image_contract` | width, height, pixel format, alpha/color, `max_encoded_bytes`, `max_decoded_bytes` |
| `member_count`, `frame_coverage` | 끝 제외 범위의 빠진 프레임 없는 coverage. halo/source 멤버는 전달 프레임 수와 별도 |
| `retention_refs` | 원본·채택·완료 빌드 참조. eviction 권한과 구분 |

index는 자기 digest를 입력에 넣지 않는다. pack hash 입력에도 index 바이트를 넣지 않는다. index가 `pack_sha256`을 담아도 순환 hash가 되지 않는다.

읽기 순서:

1. archive가 pin한 index hash를 먼저 확인한다.
2. offset/length overflow, pack bounds, header 침범, overlap, 빈틈, 중복 member id, 중복 전달 frame, 순서, 누락을 검사한다.
3. member 0의 offset은 16이고, 다음 offset은 이전 offset+length이며, 마지막 끝은 `pack_byte_length`다.
4. 필요한 member와 halo의 range만 만든다. 요청을 줄이려 무관한 큰 범위를 자동으로 읽지 않는다.
5. range 응답은 요청 범위, 반환 길이, 고정 전체 길이를 확인한다. locator가 다른 revision을 가리키면 기존 snapshot에 붙이지 않는다.
6. member 전체 바이트의 SHA-256이 index와 같은 뒤에만 decode한다.
7. 전체 pack을 모두 읽었을 때만 `VERIFIED_FULL_PACK`을 기록한다. 부분 검증은 `VERIFIED_MEMBERS`와 검증한 member/range만 기록한다.

PNG signature와 IHDR의 width/height가 `image_contract`와 다르면 decode allocation 전에 거부한다. decoded 크기는 contract의 width·height·format으로 계산하며 `max_decoded_bytes`와 남은 RAM 예약을 넘지 못한다. 손상 member를 거부할 때 원본을 지우지 않는다.

Range를 지원하지 않거나 전체 응답이 오면 member 성공으로 처리하지 않는다. cap·사용량·시간을 넘으면 stream을 멈추고 `RANGE_UNSUPPORTED` 또는 `CAPACITY_BLOCKED`로 보고한다. cap 안에서 미리 허용한 경우만 `WHOLE_PACK_VERIFIED`로 전체 SHA-256을 검사한다. 잘못된 `Content-Range`, 길이 불일치, 416은 `INPUT_MISMATCH`다. 미지원과 snapshot 불일치를 구분할 수 없으면 확인을 기다린다.

restore는 `F_000001.png`부터의 basename을 앱이 만든다. index의 경로 문자열을 목적지로 쓰지 않는다. 허용 root의 임시 경로에 쓰고 hash·형식 검사 뒤에만 공개한다. symlink, 절대 경로, 상위 경로, 승인 output 덮어쓰기를 거부한다. 전체 offline 공간 예약이 없으면 online 읽기를 offline 재현으로 표시하지 않는다. coverage·hash·기술 검사가 끝나기 전에는 복원 완료 manifest를 공개하지 않는다.

### 10.3. archive verification level

| level | 의미 | seal |
|---|---|---|
| `UPLOADED_UNVERIFIED` | 로컬 sha256·바이트, 서버 offset, 업로드 응답 | checkpoint와 완료 seal에 부족 |
| `UPLOAD_HASH_MATCHED` | 인증된 provider endpoint가 고정 object/revision에 대해 계산한 SHA-256과 length가 로컬과 일치 | v1 완료 seal의 최소. filename, MD5, ETag, 로컬 hash echo만으로는 이 level이 아니다 |
| `FULL_READBACK` | 고정 object 전체를 bounded stream으로 다시 읽어 SHA-256과 length를 대조 | provider의 신뢰 가능한 SHA-256/length가 없으면 seal에 필요. 부분 member read를 전체 readback으로 적지 않는다 |

profile이 `FULL_READBACK`을 요구하면 checksum match로 면제하지 않는다. integrity 단계와 이 level은 manifest에 따로 적는다. readback의 bytes·requests·시간·buffer는 예약에 포함하고, cap을 넘으면 완료라고 하지 않는다.

## 11. ExecutionPlan 1

경로: `execution/plan.json`.

| 필드 | 계약 |
|---|---|
| `document_type` | `execution_plan` |
| `schema_version` | 1 |
| `snapshot_digest` | 고정 입력 |
| `storage.profile` | `LOCAL_FULL` 또는 `DRIVE_BOUNDED` |
| `execution.policy` | `AUTO_PERFORMANCE` 또는 사용자가 고정한 경로 |
| `execution.allowed_routes` | 아래 route만 |
| `execution.allow_additional_charges` | 기본 false. true여도 별도 비용 승인이 있어야 한다 |
| `encoding` | driver policy, allowed drivers, DeliveryProfile, width, height, fps rational, `output_frames` |
| `workspace` | PC·worker cap과 `on_limit`. 예시의 2 GiB·8 GiB는 fixture이며 제품 기본값이나 충분 용량의 보장이 아니다 |
| `transfer_route` | `COORDINATOR_RELAY` 또는 `MANUAL_PACKET` |
| `coordinator_location` | 기본 `USER_DESKTOP` |
| `transfer_edges` | 아래 edge |
| `resource_reservations` | 위치별 peak |

`DIRECT_DRIVE`는 이 스키마의 `allowed_routes`와 `transfer_route`에 넣을 수 없다. 별도 ADR, 사용자 권한 결정, scope qualification 전에는 후보가 아니다. `PROTECTED_REMOTE_RELAY`는 이미 허용·검증된 중개 배포가 있을 때만 `coordinator_location`이 될 수 있으며, 이 ADR은 그 배포를 승인하지 않는다.

edge 필수 필드: `edge_id`, `from_role`, `to_role`, `snapshot_digest`, `object_digest`, `index_digest`, `member_ids`, `byte_ranges`, `verification`, `max_inflight_bytes`, `spool_limit_bytes`, `evidence_ref`, `measured_at`, `credential_boundary`, `expiry_binding`, `transport_retry_policy`, `dependencies`, `completion_receipt_ref`.

`evidence_ref`가 없으면 측정값은 `UNKNOWN`이다. receipt 자리만 있는 plan은 실행 완료가 아니다. 선언한 range와 실제 전송 바이트를 구분하고, retry의 중복 바이트도 모두 센다.

`transport_retry_policy`가 없으면 retry는 0이다. 있으면 `max_retries`(0 이상 3 이하), `backoff_base_ms`, `max_backoff_ms`, `max_elapsed_ms`, `max_requests`, `max_transferred_bytes`가 모두 있어야 한다. 하나라도 없으면 무제한으로 해석하지 않고 거부한다.

대기 시간은 `min(max_backoff_ms, backoff_base_ms * 2^retry_index)`이다. `retry_index`는 첫 retry가 0이다. `Retry-After`가 cap 또는 남은 allowance를 넘으면 대기 routine을 만들지 않고 중단 원인을 보인다.

이 retry는 같은 attempt 안의 idempotent GET/Range, 또는 같은 upload session의 status query에만 쓴다. 대상은 network interruption, 408, 429, 명시적 rate-limit 403, 5xx뿐이다. 401, 권한 거절 403, 404, snapshot/range/hash 불일치는 자동 retry하지 않는다. session 생성, compute submit, manifest 게시의 불명을 이 allowance로 다시 create하지 않는다.

`credential_boundary`는 비밀 없는 issuer, audience, peer, connection/account digest, `credential_epoch`, job, attempt, object/member, range, `max_bytes`, 만료다. bearer와 session 원문은 secret store에만 둔다.

자원 예약은 download chunk, 검증 보관, decode buffer, pinned cache, prefetch, output spool, mux temporary, 검사 read, whole-pack fallback 용량의 동시 생존분이다. 압축 PNG 크기와 decode 후 RGBA 크기를 서로 대체하지 않는다. cap을 맞추려 원본·프레임·품질을 지우거나 낮추지 않는다. `on_limit` 기본 의미는 `PAUSE`다.

보고 필드는 `archive_read_bytes`, `relay_to_worker_bytes`, `worker_to_relay_bytes`, `archive_write_bytes`, `verification_read_bytes`와 request 수를 나눈다.

## 12. FrameStream 1

런타임 프레임 계약이다. 저장 경로나 encoder 이름이 시간의 정본이 아니다.

각 프레임: 0-based global index, 끝 제외 범위, rational PTS, rational duration, width, height, pixel format, stride, color space, transfer, range, alpha policy, `source_digest`, `recipe_digest`.

buffer는 소유, 수명, release, GPU device/context, 동기화 fence, bounded queue를 명시한다. encoder가 소비하기 전에 buffer를 해제하지 않는다. CPU buffer, GPU surface, PNG pack은 각각 canonical pixel/color 검사를 통과해야 한다. 무손실 중간물과 전달 인코드를 분리한다. GOP reorder가 있어도 decode 순서, 표시 순서, PTS를 검사한다.

## 13. WorkerProtocol 1

job key의 입력은 snapshot, operation, 출력 범위, recipe, runtime/driver 계약이다. attempt id와 submission request id는 job key와 별도다. 불명 접수에 새 job key를 만들지 않는다.

상태: `PLANNED`, `RESERVED`, `WAITING_USER`, `SUBMITTING`, `RUNNING`, `UNKNOWN`, `CANCEL_REQUESTED`, `CANCEL_CONFIRMED`, `OUTPUT_PENDING_VERIFY`, `FAILED_CONFIRMED`, `VERIFIED`, `ARCHIVED`.

허용 전이만 유효하다.

- `PLANNED` → `RESERVED`
- `RESERVED` → `WAITING_USER`(수동) 또는 `SUBMITTING`(공식) 또는 `CANCEL_CONFIRMED`(제출 전 취소)
- `WAITING_USER` → `SUBMITTING` 또는 `CANCEL_CONFIRMED`
- `SUBMITTING` → `RUNNING`, `UNKNOWN`, `CANCEL_REQUESTED`
- `UNKNOWN` → `RUNNING`, `FAILED_CONFIRMED`, `OUTPUT_PENDING_VERIFY`, `CANCEL_CONFIRMED` (기존 작업의 확인만)
- `RUNNING` → `OUTPUT_PENDING_VERIFY`, `FAILED_CONFIRMED`, `UNKNOWN`, `CANCEL_REQUESTED`
- `OUTPUT_PENDING_VERIFY` → `VERIFIED`, `FAILED_CONFIRMED`, `UNKNOWN`, `CANCEL_REQUESTED`
- `CANCEL_REQUESTED` → `CANCEL_CONFIRMED`, `OUTPUT_PENDING_VERIFY`(완료가 먼저 확정), `UNKNOWN`
- `VERIFIED` → `ARCHIVED`

`CANCEL_REQUESTED`는 요청 전달이다. `CANCEL_CONFIRMED`는 종료 또는 미접수 확인이다. 완료가 먼저 확정되면 결과를 `OUTPUT_PENDING_VERIFY`로 검증하고, 취소했다는 이유만으로 비용·예약을 지우지 않는다. `VERIFIED` / `ARCHIVED` 이후의 취소는 과거 seal을 바꾸지 않는다.

`UNKNOWN`인 동안 예약 해제, 대체 worker, 새 attempt를 거부한다. `RUNNING`에서 온 UNKNOWN과 `OUTPUT_PENDING_VERIFY`에서 온 UNKNOWN 모두 이 fence를 쓴다. 상태 확인은 공식 완료 이벤트 또는 사용자의 한 번 이어하기다. standing polling, 자동 재시도, 소비자 UI 자동 조작은 없다.

`FAILED_CONFIRMED` 다음 attempt는 사용자가 실패, 현재 예약, 남은 allowance를 본 뒤 명시적으로 이어할 때만 시작한다. bounded retry 상한, quote, 사용권, 남은 예산을 다시 확인하고 새 attempt id와 submission request id를 남긴다. allowance 소진, 권한·가격 변경, 불명 작업이 있으면 거부한다. 이 retry는 중앙 engineering builder의 재개와 다른 계약이다.

callback은 actor, job, attempt, snapshot, 범위, request nonce에 묶고, 중복과 오래된 revision을 거부한다. worker의 COMPLETE 문장을 출력 검증으로 승격하지 않는다.

### 13.1. job_journal 1

첫 network side effect 전에 `document_type` `job_journal`, `schema_version` 1의 append-only 레코드로 `SUBMIT_INTENT`를 남긴다. 입력은 snapshot, operation, range, halo, recipe, toolchain, quote, 사용권, 예약, `credential_epoch`, job, attempt, request id다.

response는 관측이다. frame coverage와 hash는 verifier 승인으로 분리한다. 재시작은 마지막 검증 checkpoint와 미해결 intent를 복원한다. 응답이 없다는 이유로 job key나 attempt를 바꾸지 않는다. 잘리거나 중복되거나 순서가 다른 journal은 `UNKNOWN` / `RECONCILIATION_REQUIRED`다.

계산 checkpoint는 검증된 immutable artifact와 receipt다. 다시 계산할 수 있다는 사실로 paid submit의 UNKNOWN을 풀지 않는다. upload checkpoint는 secret session 참조, 로컬 object hash, server offset, 반환 object id를 나누어 적는다. offset은 archive 검증이 아니다. member/full hash가 확인되어야 archive checkpoint가 된다.

provider 문서에 idempotency key가 있어도 probe 전에는 중복 방지가 검증된 것이 아니다. 수동 packet은 사용자 import를 기다리며 자동 attach를 만들지 않는다.

## 14. EncodeRecipe 1과 driver

`document_type` `encode_recipe`, `schema_version` 1.

필드: `driver`, codec engine, container, pixel format, timebase, rate control, color, mux. driver는 `FFMPEG`, `NVIDIA_NATIVE`, `VIDEOTOOLBOX_NATIVE`, `GSTREAMER`, `QUALIFIED_SERVICE` 중 실제 probe된 것이다.

`MV_H264_AAC_V1`은 기존 전달 기준의 이름이다. H.264/AAC MP4, 프로젝트 크기, 24/1 CFR, recipe에 고정한 색, 원곡 정책. HEVC·AV1·다른 container는 사용자가 고른 다른 DeliveryProfile이다. 빠른 driver 때문에 조용히 바꾸지 않는다.

같은 CRF 숫자가 driver 사이에서 같은 품질이라는 뜻은 아니다. 비교 축은 선, 색 경계, 깜빡임, 작은 글자, 투명 경계, banding, 시간축이다. 허용 숫자를 후보마다 느슨하게 만들지 않는다. 그 숫자의 확정은 ANIM-015가 모든 driver에 같은 기준으로 고정할 일이며, 이 명세는 숫자를 발명하지 않는다.

interface는 FrameSource/Decoder, Compositor, Encoder, Muxer, MediaVerifier, ArchiveWriter로 나눈다. native encoder가 AAC·자막·container를 모두 처리한다고 가정하지 않는다. 이미 검증한 audio track을 chunk마다 다시 인코드하지 않는다.

FFmpeg 안의 hardware flag만으로 `NVIDIA_NATIVE` 완료를 기록하지 않는다. CUDA 존재가 NVENC 자격은 아니다. `NO_FFMPEG_ENCODING`의 실제 성공과, mux/verify까지 포함한 `NO_FFMPEG_RUNTIME` probe는 별도다.

fragment 인코드는 독립 decode GOP, 경계, parameter set, codec profile, timebase, color, sample description이 있을 때만 mux한다. 임의 MP4 byte concat은 완성 영상이 아니다.

## 15. CapabilityEvidence 1

`document_type` `capability_evidence`, `schema_version` 1.

필수: `evidence_id`, provider/adapter/worker digest, 비밀 없는 account binding, `credential_epoch`, session/device/OS/driver, operation, pixel/color/codec, 해상도, frame/range, 입출력·scratch·시간 cap, route/transport, fixture input/output digest, 관측 시각, 사용권 근거, 통화별 allowance, 만료·재검증 조건.

registry 상태: `DOCUMENTED_ONLY`, `QUALIFIED_FOR_SCOPE`, `STALE`, `UNAVAILABLE`. 이것은 프로그램 facet enum이 아니다. 문서나 제품 이름만으로 `QUALIFIED_FOR_SCOPE`를 만들지 않는다. 검사한 operation·format·frame 수·공간·연결 밖은 허용하지 않는다.

금지된 추론: CPU compose 성공으로 native encode, CUDA로 NVENC, 글 모델 구독으로 GPU·Drive network·상용 API credit.

session, device, driver, worker, 사용권, 계정, route, 한도, 만료가 바뀌면 기존 probe를 적용하지 않는다. UNKNOWN 작업을 새 자격 증거로 해제하지 않는다. probe의 유료 실행과 credential 발급은 별도 허용이 필요하다.

구독 allowance, compute unit, API credits, USD, 사람 hand-off 시간을 한 단위로 합치지 않는다. 포함 범위가 확인되지 않으면 추가 과금이 없는 경로로 표시하지 않는다.

`AUTO_PERFORMANCE`는 eligibility를 통과한 후보만 비교한다. 측정이 없으면 시간은 `UNKNOWN`이다. 광고 fps만으로 고르지 않는다. 측정에는 cold/warm, stage, 공유 edge, peak disk/RAM/VRAM, 전송, 사용량, 수동 시간이 같은 조건으로 묶인다.

## 16. archive seal

`document_type` `archive_seal`, `schema_version` 1.

commit key: coordinator가 선점한 build id, snapshot, 순서가 고정된 required artifact 목록, recipe, toolchain, verification profile digest.

단계: `OBJECTS_PENDING`, `OBJECTS_VERIFIED`, `MANIFEST_INTENT`, `SEALED`. 게시 불명은 `SEAL_UNKNOWN`이며 새 COMPLETE를 만들지 않는다.

완료 manifest를 공개하기 전에 필요한 원본, 최종 전달 PNG 시퀀스, clean 재현 recipe, frame_map, 원곡, cue/font, clean MP4, subbed MP4가 각각의 최소 verification level을 만족해야 한다. clean PNG 전량을 추가로 보관하는 profile은 별도 선택과 용량 예약이다. 전달 PNG가 있다는 이유만으로 두 시퀀스를 자동으로 두 번 저장하지 않는다.

manifest 바이트는 artifact hash, coverage, source snapshot, render/encode/verification 계약, journal 참조를 담는다. 자기 digest와 나중 승인 상태는 입력에 없다. 외부 승인 레코드가 manifest digest를 가리킨다. `MANIFEST_INTENT`는 네트워크 공개 전에 durable하게 남긴다. 응답을 잃으면 같은 build identity와 같은 manifest 바이트의 게시 여부를 확인한다. 이름 검색 하나만으로 일치라고 하지 않는다.

`ENCODED`, `RENDERED`, `UPLOADED`는 seal 완료가 아니다. Drive를 다중 객체 원자 커밋이나 WORM으로 가정하지 않는다. 외부 삭제·권한 회수는 `ARCHIVE_UNAVAILABLE` 또는 `INTEGRITY_FAILED`다. 과거 seal 원본은 유지한다. 실패한 job 결과와 채택 원본을 cache eviction으로 지우지 않는다.

`LOCAL_FULL`은 독립 복사와 offline replay다. `DRIVE_BOUNDED`의 replay는 archive 접근이 필요한 online replay다. offline restore는 전체 다운로드·검증·공간 확인이 별도로 성공했을 때만 offline 재현이다.

## 17. OAuth·grant 메타데이터

제품 파일에 token, refresh token, authorization code, session cookie, bearer, upload URI 원문을 저장하지 않는다.

연결 메타데이터는 `connection_id`, `account_binding_digest`, `credential_epoch`만 비밀 없이 남긴다. account binding은 이메일이나 token이 아니다.

relay grant의 비밀 없는 binding: `job_key`, `attempt_id`, `snapshot_digest`, object/member digest, read/write range, `max_bytes`, 만료, 목적 endpoint, issuer, audience, peer, `credential_epoch`. grant는 browse, 목적지 선택, 임의 URL fetch를 허용하지 않는다. 만료 갱신은 같은 job의 같은 객체만 회복하며 새 compute submit이 아니다. epoch 변경, 완료, 취소 확인 때 폐기한다.

로그아웃·권한 회수·계정 전환은 epoch를 올리고 새 read/write·submit·upload resume을 막는다. 이미 제출된 job의 UNKNOWN과 예약은 token을 지웠다는 이유만으로 `CANCEL_CONFIRMED`가 되지 않는다.

## 18. W00·경로 결정·전체 재생

`document_type` `route_decision`, `schema_version` 1.

필수: 본편 컷 id와 범위, 어려운 이유, source/sequence/artifact hash, 검토한 조건, 결정자, `decision`(`KEEP`, `CHANGE`, `MIX`), 확인한 유형과 아직 확인하지 못한 유형, 적용 범위, revision.

합성 fixture, 모델 추천, 한 장의 프레임은 이 결정을 대신하지 않는다. 실제 W00을 열 수 없으면 `acceptance_state`를 `PENDING`으로 남기고 결정을 만들어 내지 않는다. W00 결정 전에 다음 production wave를 열지 않는다.

`CHANGE` / `MIX`는 새 PLAN/WAVE revision, 영향 closure, 사용권, quote, 남은 allowance, UNKNOWN fence를 확인한다. 새 지출은 그 범위의 별도 승인이다.

전체 재생 기록은 사람이 본 build id, 파일 sha256, 방법, 문제 구간 처분, clean/subbed 기술 결과, 승인자를 적는다. 자동 의미 QC PASS 필드가 아니다.

## 19. 완료 facet 보고

프로그램 집계용 초안 표는 제품 job state나 build seal을 대체하지 않는다. host record를 이 스키마로 쓰지 않는다.

| facet | 값 |
|---|---|
| `node_state` | `NOT_STARTED`, `WAITING`, `IN_PROGRESS`, `DONE` |
| `qualification_state` | `NOT_REQUIRED`, `UNQUALIFIED`, `PARTIAL`, `QUALIFIED` |
| `acceptance_state` | `NOT_REQUIRED`, `PENDING`, `ACCEPTED`, `REJECTED` |
| `release_state` | `NOT_AUTHORIZED`, `NOT_RELEASED`, `RELEASED` |

`DONE`은 host-pinned exact-head 리뷰를 통과한 delivery가 대상에 실제로 merge된 뒤의 중앙 기록이다. `NOT_REQUIRED`는 그 node의 승인된 목적이 해당 자격이나 수용을 요구하지 않을 때만 쓴다. 환경이 없다는 이유로 `UNQUALIFIED`나 `PENDING`을 `NOT_REQUIRED`로 바꾸지 않는다.

ANIM-018의 병합 플래그 계약은 `user_merge: true`, `astra_auto_merge: false`다. 실제 허용 경로의 240초·24fps·1080p 증거와 사용자의 exact evidence 승인 전에는 merge와 `DONE`을 허용하지 않는다. 증거가 없으면 `WAITING`이다.
