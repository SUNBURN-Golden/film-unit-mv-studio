# ANIM-011 — 사용자 흐름 UI 연결: 실행 결과 기록

> ANIM-012에서 경로 B 비활성 버튼을 `fake_segment`(개발·시험용, UNQUALIFIED)의
> 실제 흐름으로 교체했다. 현재 상태는 [사용 안내](FRAME_ANIMATION_V1_USER_GUIDE_KO.md)와
> `tests/test_anim_012_ui.py`가 정본이다. 아래 경로 B 관련 기술은 ANIM-011 시점의 기록이다.

`app/control_panel.py`의 `08 · ANIMATION` 탭(모듈 `app/animation_ui.py`)은
`production_profile == "FRAME_ANIMATION_V1"` 프로젝트에서만 나타난다.
LEGACY_MV 프로젝트에는 탭도 위젯도 렌더링되지 않고 아무것도 변환하지 않는다.
모든 버튼은 ANIM-003..010 엔진 함수를 직접 호출하고, 검증은 엔진에 있다.

한 컷의 흐름: 01 준비(자산·샷 계획·패킷·총 프레임 검산) → 02 동작·노출(경로/
keypose 요약·교체 그림) → 03 import(시퀀스/draft 조립) → 04 검토(Preview·
현재 검수) → 05 수정(교체·재검수 범위) → 06 출력·승인(wave 선언·LOCK·
경로 결정·Final 후보·최종 승인). 경로 B 버튼은 `경로 B 연결 대기
(ANIM-010/012)`로 보이되 비활성이며 segment 생성을 호출하지 않는다.

## 상태 정직성

가져온 그림은 모두 DRAFT다. LOCK·검수·경로 결정·최종 승인 기록은 정확한
다이제스트에 묶인 protocol 기록이며, 실제 작품 승인이 아니다. 화면은 항상
`qualification UNQUALIFIED · acceptance PENDING · release NOT_AUTHORIZED`를
표시한다. 유료 요청·네트워크 호출·credential 발급은 없다. LOCK, 비용 승인
(B 경로 어댑터 연결 후 활성화), 최종 승인은 각각 별도 버튼·별도 사람의
기록이다.

## 성공 흐름과 오류 상태 — 커버하는 테스트

`tests/test_anim_011_ui.py`가 `streamlit.testing.v1.AppTest`로 실제
control_panel을 구동한다(합성 Pillow PNG + sine 마스터, temp
FILM_UNIT_PROJECTS).

| 상태 | 결과 | 테스트 |
|---|---|---|
| 성공: 준비→노출→import→검토→경로 결정→FINAL_LOCK→Final 후보→정확한 빌드 최종 승인 | 검수 모두 CURRENT, FINAL_FILM CURRENT, FINAL_CANDIDATE 봉인 | `test_one_cut_flow_to_final_approval` |
| 샷 계획 저장 + 제어 이미지 import | plan.json 저장, DRAFT 기록 | `test_shot_plan_upload_and_control_image` |
| 무효 계획(keypose가 컷 범위 밖) | 메시지로 거절(트레이스백 없음) | `test_invalid_plan_is_refused_with_a_message` |
| 프레임 수 불일치(10<24 프레임) | 검산이 해당 컷을 미해결로 지명 | `test_frame_count_mismatch_is_named_by_the_check` |
| 없는 자산 참조 핀 | `REFERENCE_UNKNOWN` 거절 | `test_missing_asset_reference_is_refused` |
| 잠기지 않은 wave scope에서 컷 검수 | `locked wave scope` 거절 | `test_cut_review_refused_outside_a_locked_wave` |
| 컷 교체 후 낡은 검수·WAVE_LOCK | 재검수 범위 표시; 잠긴 scope 편집 거절; 재LOCK+재검수로 복구 | `test_replacement_stales_reviews_and_lock_then_recovers` |
| Draft/외부 빌드 승인 시도 | `최종 승인 대상이 아닙니다` 거절 | `test_final_approval_refuses_a_foreign_build` |
| 빌드 후 컷 교체(낡은 빌드) 승인 | stale/검수 거절 | `test_final_approval_blocked_when_the_build_is_stale` |
| 경로 B | 버튼 비활성·정확한 라벨·segment_jobs 미생성 | `test_path_b_button_is_present_disabled_and_calls_nothing` |
| LEGACY_MV | ANIMATION 탭·위젯 없음, 기존 UI 그대로 | `test_legacy_mv_project_shows_no_animation_section` + 기존 `tests/test_ui*.py` |

## fake/합성 상태

테스트의 모든 자산·검수·LOCK·경로 결정·최종 승인은 합성 fixture가 만든
protocol 기록이다. 실제 작품 제작·승인, qualification, release로 집계하지
않는다 — acceptance는 PENDING, release는 NOT_AUTHORIZED로 유지된다.
