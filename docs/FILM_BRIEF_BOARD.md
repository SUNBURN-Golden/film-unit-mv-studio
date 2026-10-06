# 제작 준비 보드 (film-brief-board)

처음 작업을 맡겼을 때 필요한 자료와 미확정 사항을 한곳에서 정리하는 화면입니다.
기존 프로젝트 입력만 사용하며 별도 저장소를 만들지 않습니다.

## 화면과 모듈

- `app/control_panel.py`의 **01 · 제작 준비** 탭 → `app/brief_board.py`
- `app/brief_board.py`는 `engine/brief.py`의 함수를 호출만 합니다(검증 로직을 복제하지 않음).
- 새 프로젝트 폼도 `engine/brief.py`의 `create_project`를 거치며 `원하는 감정` 입력이 추가되었습니다.

## 임시 자료와 채택 입력

| 구분 | 위치 | 규칙 |
|---|---|---|
| 임시 자료 (올리기만 한 것) | `brief/staging/` | 어떤 fingerprint·검수·컴파일에도 들어가지 않습니다. 삭제 가능. |
| 채택 입력 | `input/master.*`, `input/brief.md`, `input/lyrics.txt`, `input/references/`, `project.yaml`의 `direction.emotion` | 버튼으로 명시 채택. `brief/board.json`에 채택 digest·시각이 기록됩니다. |

레퍼런스는 로컬 파일·메모(붙여넣기)만 받습니다. 앱이 외부 링크를 가져오지 않습니다.
`input/references/`는 제작 의도 자료이며 샷별 `references`·검수 binding과 별개입니다.

## 원곡·원문 변경의 영향

- 원곡은 측정된 타임라인에 묶여 있습니다. `analysis/audio.json`, `manifest/shots.json` 또는
  `timeline/edit.json`이 있으면 다른 곡으로 교체를 거부하고 새 프로젝트를 안내합니다.
  같은 바이트는 삭제된 원곡 복원에만 쓸 수 있습니다. 분석 전 교체는 이전 원곡을
  `input/master-superseded-*`로 보존합니다(확장자가 달라도 이전 파일을 보존합니다).
  합성 테스트 음원을 같은 바이트로 복원하면 합성 슬레이트 표시가 유지되며, 새로 올린
  파일만 실제 음원으로 기록됩니다.
- 가사 원문·brief를 바꾸면 화면의 **변경 영향**이 `production_fingerprint`,
  `visual_context_fingerprint`, 리뷰 binding을 다시 계산해 전체 LOCK·FRAME_ANIMATION_V1의
  PLAN/WAVE/FINAL LOCK·샷별 영상 검수·가사 검수 중 재검수가 필요한 항목을 보여줍니다.
  보고는 읽기 전용이라 탭을 여는 것만으로 프로젝트가 바뀌지 않습니다. 기존 LOCK·검수
  기록·교체된 타이밍(`lyrics/history/`)은 그대로 보존되며 새 문서에 승계하지 않습니다.
- 가사 글자 수나 곡 길이로 보컬 타이밍을 추정하지 않습니다. 새 원문은 cue 없이
  미해결 행으로만 들어갑니다.

## 입력 검증과 표시

- 지원 음원: MP3 / WAV, 1초–10분 (검증 기준 240초 · 24fps). 형식 오류·손상·무음원·
  길이 초과·중복 import(같은 digest)는 각각 명확한 메시지로 거부됩니다.
- 음원 없음·digest 불일치·합성 테스트 음원·임시 슬레이트(placeholder) 샷 수·미확정
  항목이 화면에 표시됩니다.
- 새 프로젝트 생성은 기존 디렉터리를 덮어쓰지 않습니다(`init_project`의 거부).

## 시나리오와 테스트

| 시나리오 | 테스트 |
|---|---|
| 임시 자료가 fingerprint·빌드에 들어가지 않음 | `test_staging_records_digest_and_never_touches_fingerprints`, `test_temporary_materials_never_reach_a_build` |
| 중복 import (같은 digest) | `test_duplicate_stage_is_refused`, `test_staging_the_adopted_master_bytes_is_a_duplicate`, `test_stage_duplicate_import_shows_a_message`, `test_adopt_audio_twice_is_a_duplicate`, `test_adopt_same_text_is_a_duplicate`, `test_adopting_identical_text_shows_duplicate` |
| 잘못된 음원 (형식·손상·무음원·길이) | `test_validate_audio_rejects_*`, `test_unsupported_and_corrupt_audio_adoption_is_refused` |
| 측정 후 다른 곡 교체 거부·원곡 보존·복원 | `test_adopt_audio_refuses_a_different_song_once_measured`, `test_adopt_audio_restores_missing_master_but_refuses_another` |
| 합성 음원 복원 시 슬레이트 유지·새 업로드는 실제 음원 | `test_readopting_identical_synthetic_bytes_keeps_the_slate`, `test_adopt_audio_marks_new_uploads_real_but_keeps_marked_synthetic` |
| 다른 확장자 원곡 교체 시 이전 파일 보존 | `test_adopt_audio_preserves_previous_master_across_suffix_change` |
| PLAN_LOCK 변경 영향·보고의 무결성(읽기 전용) | `test_impact_report_names_stale_plan_lock`, `test_impact_report_is_side_effect_free` |
| 임시 슬레이트(placeholder) 샷 표시 | `test_placeholder_storyboards_are_reported_as_slates` |
| 분석 전 원곡 교체와 이전 원곡 보존 | `test_adopt_audio_replaces_master_before_analysis_and_preserves_old` |
| 원문 변경 영향 (LOCK·영상 검수·가사 검수 stale) | `test_change_impact_reports_stale_lock_reviews_and_cues`, `test_lyrics_adoption_never_invents_timing_and_shows_impact` |
| 가사 길이로 타이밍을 만들지 않음 | `test_adopting_lyrics_never_derives_timing_from_length` |
| 새 프로젝트가 기존 package를 덮어쓰지 않음 | `test_create_project_refuses_to_overwrite_an_existing_package`, `test_name_collision_never_overwrites_an_existing_package` |
| 누락 음원·빈 값 | `test_missing_master_is_reported_not_hidden`, `test_missing_master_is_shown_on_the_board`, `test_empty_form_shows_a_message` |
| 채택 digest 기록·임시/채택 구분 | `test_create_project_registers_adopted_inputs`, `test_adopt_reference_records_digest_and_rejects_duplicates`, `test_stage_then_adopt_reference`, `test_brief_emotion_and_lyrics_adoption_from_the_board` |
| 지원 길이·format·임시 slate 상태 표시 | `test_board_status_shows_limits_pending_and_slate_state`, `test_board_tab_lists_adopted_inputs_limits_and_slate_state` |

## 검증 범위

테스트는 `streamlit.testing.v1` AppTest로 실제 `app/control_panel.py`를 헤드리스로
구동합니다. 모든 위젯은 레이블이 있는 표준 Streamlit 컨트롤이라 키보드 이동이
동작합니다(커스텀 JS 없음). 실제 브라우저의 정상/빈 값/오류/진행 중 화면과 키보드
동작 확인은 이 박스에 실환경이 없어 미검증(UNQUALIFIED)으로 남기며, 유지자가 별도로
기록합니다. 이 보드는 유료 생성·계정 동의·작품 승인을 만들거나 승계하지 않습니다.
