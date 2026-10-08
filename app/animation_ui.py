"""FRAME_ANIMATION_V1 one-cut flow (ANIM-011; design 14절).

The "08 · ANIMATION" tab of the existing control panel — rendered only for
projects whose production_profile is FRAME_ANIMATION_V1; LEGACY_MV projects
render exactly as before and nothing here converts a project. Each button
calls the ANIM-003..010 engine functions directly; the panel re-implements
no validation. Imported art stays DRAFT and every lock/review/decision is a
protocol record bound to exact digests — qualification UNQUALIFIED,
acceptance PENDING, release NOT_AUTHORIZED; the panel never implies
production release. The path-B (ANIM-010) section at the bottom is wired to
the explicit dev/test adapter `fake_segment` (UNQUALIFIED) only: control
inputs → capability preflight → quote/reservation → a separate cost
approval → submit → same-identity status/reconcile → result verification →
draft import → commit. Real providers stay declaration-only and refuse
submission; UNKNOWN and cancel-race outcomes are fenced (no polling, no
automatic retry, no substitute submission).
"""
from pathlib import Path
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.animation_assets import (commit_draft_frames, import_draft_image,
                                     import_frame_sequence, import_layer_rgba,
                                     import_mask, import_replacement_drawing,
                                     import_rig_spec, load_registry)
from engine.animation_compiler import compile_final_candidate
from engine.animation_locks import (WAVES_PATH, declare_waves, load_waves,
                                    lock_status, record_final_lock,
                                    record_plan_lock, record_route_decision,
                                    record_wave_lock, route_status)
from engine.animation_preview import compile_draft_preview
from engine.animation_review import (film_review_status, record_cut_review,
                                     record_film_review,
                                     record_transition_review, review_status)
from engine.animation_schema import load_animation_timeline
from engine.builds import list_builds
from engine.compositor import composite_shot
from engine.core import FilmError, project_mutex, safe_path
from engine.frame_sequence import animation_validate
from engine.motion_plan import load_shot_plan, plan_path, save_shot_plan
from engine.packets import export_work_packets
from engine.segment_fake import make_adapter
from engine.segment_gen import (commit_segment_sequence,
                                import_segment_control, load_job_journal,
                                segment_cancel, segment_import, segment_jobs,
                                segment_quote, segment_reconcile,
                                segment_submit)
from engine.w00_gate import (DELIVERABLES, DELEGATION_SCOPES,
                             approve_delivery, deliverable_status,
                             gate_status, record_delegation, record_pilot,
                             record_spend_approval)

CONTROL_ROLES = ["layout", "keypose", "breakdown", "pose", "first_frame",
                 "inbetween"]
STATE_KO = {"CURRENT": "현재", "STALE": "낡음", "UNREVIEWED": "미검수",
            "UNRESOLVED": "미해결", "CHANGES_REQUIRED": "수정 필요",
            "UNLOCKED": "미잠금"}


def _jsonable(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _guarded(fn):
    """Run one engine call; a refusal becomes a message, not a traceback."""
    try:
        return fn()
    except FilmError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"처리하지 못했습니다: {exc}")
    return None


def _do(fn, message):
    """Run a mutating engine action; on success flash it and redraw."""
    result = _guarded(fn)
    if result is not None:
        st.session_state["an_flash"] = message
        st.session_state["an_detail"] = result
        st.rerun()
    return result


def _csv(text):
    return [s.strip() for s in text.split(",") if s.strip()]


def _pins(text):
    """Parse `asset_id:revision:sha256` pins separated by commas."""
    pins = []
    for raw in _csv(text):
        parts = raw.split(":")
        if len(parts) != 3:
            raise FilmError("참조 핀은 asset_id:revision:sha256 형식입니다")
        pins.append({"asset_id": parts[0], "revision": int(parts[1]),
                     "content_sha256": parts[2]})
    return pins


def _upload_name(upload):
    """A browser-supplied file name confined to a plain basename.

    The name is untrusted: an absolute path or `..` segment would escape the
    temp directory, so only the final component is kept (and a hidden or
    empty name gets a fixed fallback).
    """
    name = Path(str(upload.name).replace("\\", "/")).name
    if not name or name.startswith(".") or name in {"..", "."}:
        name = "upload.bin"
    return name


def _sequence_import(p, shot_id, files):
    """Store uploaded PNGs as an ordered folder and import the sequence."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for index, uploaded in enumerate(files):
            (folder / f"f{index:04d}.png").write_bytes(uploaded.getvalue())
        return import_frame_sequence(p, shot_id, folder=folder)


def _registry(p):
    try:
        return load_registry(p)
    except FilmError as exc:
        st.error(str(exc))
        return {"assets": {}, "assignments": {}}


def _layer_pins(registry):
    """Current LAYER_RGBA revision pins, keyed by a display label."""
    out = {}
    for asset_id, entry in sorted(registry.get("assets", {}).items()):
        record = entry["revisions"][str(entry["current_revision"])]
        if record["kind"] == "LAYER_RGBA":
            pin = {"asset_id": asset_id,
                   "revision": entry["current_revision"],
                   "content_sha256": record["content_sha256"]}
            out[f"{asset_id} r{entry['current_revision']}"] = \
                (pin, record.get("pivot", [0, 0]))
    return out


def _stale_lock_rows(locks):
    rows = [("PLAN_LOCK", locks["plan"])]
    rows += [(f"WAVE_LOCK {w}", s) for w, s in locks["waves"].items()]
    rows.append(("FINAL_LOCK", locks["final"]))
    return [f"{name} · {s['state']}"
            + (f" (바뀐 범위: {', '.join(s['changed'])})" if s["changed"] else "")
            for name, s in rows if s["state"] == "STALE"]


# ---------------------------------------------------------------------------
# Flow steps


def _prepare(p, shot_id):
    st.caption("자산·샷 계획·작업 패킷을 준비하고 총 프레임 수를 검산합니다.")
    st.markdown("**자산 가져오기 (레이어·마스크·리그)**")
    kind = st.selectbox("자산 종류", ["LAYER_RGBA", "MASK", "RIG_SPEC"],
                        key="an_asset_kind")
    asset_file = st.file_uploader("자산 파일 (PNG / RIG_SPEC은 JSON)",
                                  type=["png", "json"],
                                  accept_multiple_files=True,
                                  key="an_asset_file")
    if kind == "LAYER_RGBA":
        c1, c2, c3 = st.columns(3)
        pivot = [c1.number_input("pivot x", value=0, key="an_asset_px"),
                 c2.number_input("pivot y", value=0, key="an_asset_py")]
        crop = [c3.number_input("crop x", value=0, key="an_asset_cx"),
                st.number_input("crop y", value=0, key="an_asset_cy")]
        z_order = st.number_input("z-order", value=0, key="an_asset_z")
    elif kind == "MASK":
        layers = _layer_pins(_registry(p))
        mask_target = st.selectbox("마스크 대상 레이어", list(layers),
                                   key="an_mask_target")
        channel = st.radio("채널", ["LUMINANCE", "ALPHA"], horizontal=True,
                           key="an_mask_channel")
        region_text = st.text_input("영역 x,y,w,h (비우면 전체)",
                                    key="an_mask_region")
    if st.button("자산 import", key="an_asset_import", disabled=not asset_file):
        def run():
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / _upload_name(asset_file[0])
                source.write_bytes(asset_file[0].getvalue())
                if kind == "LAYER_RGBA":
                    return import_layer_rgba(
                        p, source, pivot=[int(v) for v in pivot],
                        crop_origin=[int(v) for v in crop],
                        z_order=int(z_order))
                if kind == "MASK":
                    if not mask_target:
                        raise FilmError("먼저 LAYER_RGBA 자산을 가져오세요.")
                    region = ([int(v) for v in _csv(region_text)]
                              if region_text.strip() else None)
                    return import_mask(p, source,
                                       target=layers[mask_target][0],
                                       region=region, channel=channel)
                return import_rig_spec(
                    p, json.loads(source.read_text(encoding="utf-8")))
        _do(run, f"{kind} 자산을 DRAFT로 가져왔습니다")
    st.divider()
    plan_up = st.file_uploader("샷 계획 JSON (animation_shot_plan)",
                               type=["json"], accept_multiple_files=True,
                               key="an_plan_file")
    if st.button("샷 계획 저장", key="an_plan_save", disabled=not plan_up):
        def run():
            document = json.loads(plan_up[0].getvalue().decode("utf-8"))
            return save_shot_plan(p, document)
        _do(run, f"{shot_id} 계획을 저장했습니다")
    if st.button("A 경로 작업 패킷 만들기", key="an_packet"):
        _do(lambda: export_work_packets(p, shots=[shot_id]),
            f"{shot_id} 작업 패킷을 썼습니다 (수동 hand-off용)")
    st.markdown("**제어·제작 이미지 가져오기**")
    ctrl_file = st.file_uploader("이미지 (PNG)", type=["png"],
                                 accept_multiple_files=True,
                                 key="an_ctrl_file")
    role = st.selectbox("역할", CONTROL_ROLES, key="an_ctrl_role",
                        help="layout은 조건 입력만; 나머지는 패킷 요청과 맞아야 합니다")
    ctrl_frame = st.number_input("컷 기준 프레임", min_value=0, value=0,
                                 key="an_ctrl_frame")
    ctrl_refs = st.text_input("참조 핀 (asset_id:revision:sha256 — 쉼표 구분)",
                              key="an_ctrl_refs")
    if st.button("제어 이미지 import", key="an_ctrl_import",
                 disabled=not ctrl_file):
        def run():
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / _upload_name(ctrl_file[0])
                source.write_bytes(ctrl_file[0].getvalue())
                return import_draft_image(
                    p, shot_id, source, role=role, frame=int(ctrl_frame),
                    references=_pins(ctrl_refs))
        _do(run, f"{shot_id} 프레임 {int(ctrl_frame)} · {role} — DRAFT로 기록했습니다")
    st.divider()
    if st.button("계획 검사 · 총 프레임 검산", key="an_validate"):
        result = _guarded(lambda: animation_validate(p))
        if result is not None:
            tl = result["timeline"]
            cols = st.columns(4)
            cols[0].metric("출력 프레임", tl["output_frames"])
            cols[1].metric("목표 프레임", result["target_frames"])
            cols[2].metric("겹침", tl["overlap_frames"])
            cols[3].metric("전환", len(tl["transitions"]))
            coverage = tl["coverage"]
            if coverage["uncovered_frames"]:
                st.error("커버되지 않는 출력 프레임: "
                         f"{coverage['uncovered_frames']}개")
            if coverage["frames_with_three_or_more"]:
                st.error("세 컷 이상이 겹치는 프레임이 있습니다: "
                         f"{coverage['frames_with_three_or_more']}개")
            if result["unresolved"]:
                reasons = "; ".join(
                    f"{e['instance_id']}({e['shot_id']}): {e['reason']}"
                    for e in result["entries"] if not e["resolved"])
                st.error("시퀀스가 해결되지 않는 컷이 있습니다: " + reasons)
            elif result["ok"]:
                st.success("모든 컷의 시퀀스가 해결되고 총 프레임이 일치합니다.")
            with st.expander("검사 상세 (해시 포함)"):
                st.json(_jsonable(result))


def _motion(p, shot_id):
    st.caption("저장된 계획의 경로·keypose·노출 요약을 보고 교체 그림을 넣습니다.")
    if safe_path(p, plan_path(shot_id)).is_file():
        plan = _guarded(lambda: load_shot_plan(p, shot_id))
        if plan:
            st.dataframe(
                [{"구간": f"[{s['start']}, {s['end']})", "경로": s["path"],
                  "능력": ", ".join(s["capabilities"])}
                 for s in plan["segments"]], hide_index=True)
            if plan["keyposes"]:
                st.dataframe(
                    [{"프레임": k["frame"], "종류": k["kind"]}
                     for k in plan["keyposes"]], hide_index=True)
            camera = plan["tracks"]["camera"]
            st.caption("카메라: " + ("움직임 있음" if camera["transform"]
                                     else "고정"))
            layers = plan["tracks"]["layers"]
            if layers:
                st.dataframe(
                    [{"레이어": lid, "그림 수": len(track["drawings"]),
                      "노출 스케줄": len(track["exposure"]),
                      "움직임": "있음" if track["transform"] else "정지",
                      "마스크": "있음" if track["mask"] else "없음"}
                     for lid, track in layers.items()], hide_index=True)
    else:
        st.caption(f"{shot_id}에 저장된 샷 계획이 없습니다.")
    st.markdown("**교체 그림 (REPLACEMENT_DRAWING)**")
    layers = _layer_pins(_registry(p))
    if not layers:
        st.caption("교체 대상 LAYER_RGBA가 없습니다. 01 · 준비에서 레이어를 "
                   "먼저 가져오세요.")
        return
    target = st.selectbox("대상 레이어", list(layers), key="an_repl_target")
    pin, pivot = layers[target]
    rep_frame = st.number_input("처음 나타나는 컷 프레임", min_value=0,
                                value=0, key=f"an_repl_frame_{target}")
    c1, c2 = st.columns(2)
    rep_pivot = [c1.number_input("pivot x", value=pivot[0],
                                 key=f"an_repl_px_{target}"),
                 c2.number_input("pivot y", value=pivot[1],
                                 key=f"an_repl_py_{target}")]
    rep_file = st.file_uploader("교체 그림 (RGBA PNG)", type=["png"],
                                accept_multiple_files=True,
                                key=f"an_repl_file_{target}")
    if st.button("교체 그림 import", key=f"an_repl_import_{target}",
                 disabled=not rep_file):
        def run():
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / _upload_name(rep_file[0])
                source.write_bytes(rep_file[0].getvalue())
                return import_replacement_drawing(
                    p, source, replaces=pin, frame=int(rep_frame),
                    pivot=[int(v) for v in rep_pivot])
        _do(run, f"{target}의 교체 그림을 DRAFT로 가져왔습니다")


def _import(p, shot_id):
    st.caption("외부에서 만든 프레임을 시퀀스로 가져오거나, 패킷으로 확인한 "
               "draft 프레임을 시퀀스로 조립합니다. 둘 다 DRAFT입니다.")
    seq = st.file_uploader("프레임 PNG 묶음 (업로드 순서=프레임 순서)",
                           type=["png"], accept_multiple_files=True,
                           key="an_seq_files")
    if st.button("시퀀스 import", key="an_seq_import", disabled=not seq):
        _do(lambda: _sequence_import(p, shot_id, seq),
            f"{shot_id} 시퀀스 {len(seq)}프레임을 DRAFT로 가져왔습니다")
    if st.button("draft 프레임 조립 (commit-draft-frames)",
                 key="an_draft_commit"):
        _do(lambda: commit_draft_frames(p, shot_id),
            f"{shot_id}의 draft 프레임을 시퀀스로 조립했습니다")


def _review(p, shot_id, entry, entries, reviews):
    st.caption("컷/전체 Preview를 재생하고 현재 버전의 검수를 기록합니다.")
    c1, c2 = st.columns(2)
    if c1.button("이 컷 합성 Preview (C 계획)", key="an_shot_preview"):
        if not safe_path(p, plan_path(shot_id)).is_file():
            st.error(f"{shot_id}에 저장된 샷 계획이 없습니다 — "
                     "compile-shot은 C 계획 컷 전용입니다. A 경로/가져온 "
                     "시퀀스는 곡 전체 Draft Preview로 확인하세요.")
        else:
            result = _guarded(lambda: composite_shot(p, shot_id))
            if result is not None:
                st.success(f"{shot_id} 합성 시퀀스를 만들었습니다 "
                           f"(DRAFT)")
                with st.expander("합성 결과 상세"):
                    st.json(_jsonable(result))
    if c2.button("곡 전체 Draft Preview", key="an_preview"):
        with st.spinner("Draft Preview를 컴파일하고 있습니다…"):
            result = _guarded(lambda: compile_draft_preview(p))
        if result is not None:
            st.success(f"{result['build_id']} · DRAFT_PREVIEW "
                       f"({result['frames']['resolved_frames']}/"
                       f"{result['frames']['total']} 프레임 해결)")
            for warning in result["warnings"]:
                st.warning(warning)
            st.video(result["output"])
    st.markdown("**현재 검수 상태**")
    st.dataframe(
        [{"대상": t, "범위": row["scope"],
          "상태": STATE_KO.get(row["state"], row["state"]),
          "리뷰": row.get("review_id") or "—"}
         for t, row in reviews["targets"].items()],
        hide_index=True)
    reviewer = st.text_input("검토자", key="an_reviewer")
    current = reviews["targets"].get(entry["instance_id"], {})
    st.caption(f"{entry['instance_id']} ({shot_id}) 현재 검수: "
               f"{STATE_KO.get(current.get('state'), '미검수')}")
    c1, c2 = st.columns(2)

    def cut_review(decision):
        with project_mutex(p):
            return record_cut_review(
                p, entry["instance_id"], reviewer=reviewer,
                methods=["CUT_FULL_SPEED_PLAYBACK"], decision=decision)
    if c1.button("이 컷 승인 (현재 버전 검토 완료)", key="an_cut_ok"):
        _do(lambda: cut_review("APPROVED"),
            f"{entry['instance_id']} 승인을 기록했습니다 (protocol 검수)")
    if c2.button("이 컷 수정 필요 기록", key="an_cut_fix"):
        _do(lambda: cut_review("FIX_REQUIRED"),
            f"{entry['instance_id']} 수정 필요를 기록했습니다")
    transitions = [e["transition_out"]["id"] for e in entries
                   if e.get("transition_out")]
    if transitions:
        pick = st.selectbox("전환", transitions, key="an_tr_pick",
                            format_func=lambda t: f"{t} · {STATE_KO.get(reviews['targets'].get(t, {}).get('state'), '미검수')}")
        if st.button("이 전환 승인", key="an_tr_ok"):
            def run():
                with project_mutex(p):
                    return record_transition_review(
                        p, pick, reviewer=reviewer,
                        methods=["TRANSITION_FULL_SPEED_PLAYBACK"])
            _do(run, f"{pick} 전환 승인을 기록했습니다")


def _fix(p, shot_id, locks, reviews):
    st.caption("한 컷의 프레임·그림을 교체하면 그 컷에 묶인 검수·LOCK이 "
               "낡아집니다. 영향 범위를 먼저 확인하세요.")
    stale = [f"{t} · {STATE_KO.get(r['state'], r['state'])}"
             for t, r in reviews["targets"].items()
             if r["state"] in ("STALE", "CHANGES_REQUIRED")]
    missing = [t for t, r in reviews["targets"].items()
               if r["state"] in ("UNREVIEWED", "UNRESOLVED")]
    stale_locks = _stale_lock_rows(locks)
    if stale or stale_locks:
        st.warning("재검수 필요: " + ("; ".join(stale + stale_locks)))
    elif missing:
        st.info("아직 검수되지 않은 대상: " + "; ".join(missing))
    else:
        st.success("모든 검수와 LOCK이 현재 버전을 가리킵니다.")
    with st.expander("교체 영향 미리보기 (rebuild-plan)", expanded=False):
        _replacement_projection(p, shot_id)
    seq = st.file_uploader("교체 프레임 PNG 묶음", type=["png"],
                           accept_multiple_files=True, key="an_fix_files")
    if st.button("이 컷 교체 (새 리비전 import)", key="an_fix_import",
                 disabled=not seq):
        _do(lambda: _sequence_import(p, shot_id, seq),
            f"{shot_id}를 새 리비전으로 교체했습니다 — 묶여 있던 검수는 "
            "재검수가 필요합니다")


def _replacement_projection(p, shot_id):
    """ANIM-020 stale-approval projection for the fix panel's actual edit —
    a whole-sequence replacement of this shot (rebuild-plan, nothing runs)."""
    from engine.render_provenance import plan_rebuild
    try:
        iid = next((e["instance_id"] for e in load_animation_timeline(p)
                    ["entries"] if e["shot_id"] == shot_id), None)
        if iid is None:
            st.caption(f"{shot_id}는 타임라인에 없습니다.")
            return
        plan = plan_rebuild(p, [{"class": "SEQUENCE", "instance_id": iid}])
    except FilmError as exc:
        st.caption(f"영향 예측을 만들 수 없습니다: {exc}")
        return
    closure, projection = plan["closure"], plan["stale_approval_projection"]
    st.caption(f"{iid} 전체 교체 시 재합성 {len(closure['dirty_frames'])}프레임 "
               f"(범위 {closure['compose_ranges']}), 읽을 멤버 "
               f"{sum(len(v) for v in closure['needed_members'].values())}개 "
               f"(halo {sum(len(v) for v in closure['halo_members'].values())}개), "
               f"재인코딩 {', '.join(closure['encode_roles']) or '없음'}")
    if projection["stale"]:
        st.warning("낡아지는 검수·LOCK: " + ", ".join(projection["stale"]))
    if projection["kept"]:
        st.caption("유지: " + ", ".join(projection["kept"]))


def _waves(p, entries):
    waves_doc = _guarded(lambda: load_waves(p))
    if waves_doc is not None:
        st.dataframe(
            [{"wave": w["wave"], "샷": ", ".join(w["shots"]),
              "어려운 유형": ", ".join(d["type"] for d in w["difficulty"])
              or "—", "메모": w.get("note", "")}
             for w in waves_doc["waves"]], hide_index=True)
        return waves_doc
    if safe_path(p, WAVES_PATH).exists():
        return None  # unreadable file: the error was already shown
    st.markdown("**제작 순서 (wave) 선언**")
    st.caption("W00은 실제 본편 컷 중 가장 어려운 것을 먼저 만드는 초기 "
               "wave입니다. 나머지 샷은 W01로 둡니다.")
    shots = [e["shot_id"] for e in entries]
    w00 = st.multiselect("W00 샷", shots, default=shots[:1],
                         key="an_w00_shots")
    types = st.text_input("W00이 검증할 어려운 유형 (쉼표 구분)",
                          key="an_w00_types")
    reason = st.text_input("유형을 고른 근거", key="an_w00_reason")
    if st.button("제작 순서 선언", key="an_waves_declare"):
        def run():
            if not w00:
                raise FilmError("W00에 넣을 샷을 고르세요")
            difficulty = [{"type": t, "reason": reason.strip()}
                          for t in _csv(types)]
            rest = [s for s in shots if s not in w00]
            waves = [{"wave": "W00", "shots": list(w00),
                      "difficulty": difficulty, "note": "control panel 선언"}]
            if rest:
                waves.append({"wave": "W01", "shots": rest,
                              "difficulty": [], "note": "나머지 범위"})
            return declare_waves(p, waves)
        _do(run, "제작 순서를 선언했습니다")
    return None


def _locks(p, waves_doc):
    st.markdown("**범위 LOCK**")
    st.caption("LOCK·비용 승인·최종 승인은 각각 별도 버튼과 별도 사람의 "
               "기록입니다. 이 화면의 A 경로 수작업에는 유료 생성이 없어 "
               "비용 승인 단계가 없습니다 (B 경로 견적 승인은 어댑터 연결 "
               "후에 활성화됩니다).")
    approver = st.text_input("범위 승인자", key="an_lock_approver")
    c1, c2, c3 = st.columns(3)
    if c1.button("PLAN_LOCK", key="an_lock_plan"):
        _do(lambda: record_plan_lock(p, approver),
            "PLAN_LOCK을 기록했습니다")
    wave_ids = [w["wave"] for w in waves_doc["waves"]] if waves_doc else []
    wave_pick = c2.selectbox("wave", wave_ids, key="an_lock_wave_pick")
    if c2.button("WAVE_LOCK", key="an_lock_wave", disabled=not wave_ids):
        _do(lambda: record_wave_lock(p, wave_pick, approver),
            f"{wave_pick} WAVE_LOCK을 기록했습니다")
    if c3.button("FINAL_LOCK", key="an_lock_final"):
        _do(lambda: record_final_lock(p, approver),
            "FINAL_LOCK을 기록했습니다")


def _route(p, waves_doc, route):
    st.markdown("**초기 wave 경로 결정**")
    if route["checkpoint"] == "DECIDED":
        decision = route["decision"]
        st.success(f"{decision['decision_id']} · {decision['wave']} · "
                   f"{decision['decision']} · 적용 범위: "
                   f"{decision['apply_scope']['waves']}")
        return
    if waves_doc is None:
        st.caption("먼저 제작 순서를 선언하세요.")
        return
    initial = waves_doc["waves"][0]
    declared = [d["type"] for d in initial["difficulty"]]
    later = [w["wave"] for w in waves_doc["waves"][1:]]
    decision = st.radio("결정", ["keep", "change", "mix"], horizontal=True,
                        key="an_rd_decision")
    c1, c2 = st.columns(2)
    decider = c1.text_input("결정자", key="an_rd_decider")
    approver = c2.text_input("결정 승인자", key="an_rd_approver")
    checked = st.multiselect("검증한 유형", declared, default=declared,
                             key="an_rd_checked")
    scope = st.multiselect("적용 범위로 여는 이후 wave", later, default=later,
                           key="an_rd_scope")
    conditions = st.text_input("검토한 조건 (쉼표 구분)", key="an_rd_conditions")
    observations = st.text_input("관측 근거 (쉼표 구분)", key="an_rd_observations")
    changes = st.text_input("변경 항목 — CHANGE/MIX (쉼표 구분)",
                            key="an_rd_changes")
    cost = st.text_input("비용·시간 영향", key="an_rd_cost")
    early = st.checkbox("조기 결정 (모든 컷 채택 전)", key="an_rd_early")
    grounds = st.text_input("조기 결정 근거", key="an_rd_grounds")
    if st.button("경로 결정 기록", key="an_rd_record"):
        def run():
            return record_route_decision(
                p, initial["wave"], decision=decision, decider=decider,
                approver=approver, checked_types=list(checked),
                unchecked_types=[t for t in declared if t not in checked],
                apply_scope=list(scope),
                reviewed_conditions=_csv(conditions),
                observations=_csv(observations), changes=_csv(changes),
                cost_time_impact=cost, early=early, grounds=grounds)
        _do(run, f"{initial['wave']} 경로 결정을 기록했습니다")


def _approve_film(p, build, reviewer, decision):
    """UI gate for approve-film: exact FINAL_CANDIDATE build + current
    (non-stale) cut/transition reviews; the engine binding is recorded by
    record_film_review itself."""
    build_id = build["build_id"]
    if (build.get("document_type") != "animation_build"
            or build.get("schema_version") != 2
            or build.get("status") != "COMPLETE"
            or build.get("mode") != "FINAL_CANDIDATE"):
        raise FilmError(
            f"{build_id}는 최종 승인 대상이 아닙니다 — FINAL_CANDIDATE로 "
            "봉인된 정확한 Build 2만 승인할 수 있습니다")
    reviews = review_status(p, strict=False)
    if build.get("edit_digest") != reviews["edit_digest"]:
        raise FilmError(
            f"{build_id}는 지금 편집과 다른 상태에서 만들어진 빌드입니다 "
            "(stale). 새 Final 후보를 만들어 주세요.")
    pending = [f"{t} {r['state']}" for t, r in reviews["targets"].items()
               if r["state"] != "CURRENT"]
    if pending:
        raise FilmError("현재 검수가 아닌 대상이 있어 최종 승인을 기록할 수 "
                        "없습니다: " + "; ".join(pending))
    with project_mutex(p):
        return record_film_review(
            p, build_id, reviewer=reviewer,
            methods=["FULL_SPEED_WHOLE_FILM", "TECHNICAL_VALIDATION"],
            deliverable="MASTER_SUBBED.mp4", decision=decision)


def _w00_gate(p):
    """W00 pilot records + the next-wave gate (ANIM-022; design 10.5).

    Lists the initial wave's pilot targets with their record state, takes a
    per-target KEEP/CHANGE/MIX pilot decision, and shows the gate with its
    reasons. Synthetic reviewer records are labelled SYNTHETIC_FIXTURE and
    imply no real W00 approval — the facets stay PENDING/UNQUALIFIED.
    """
    st.markdown("**W00 파일럿 기록 · 다음 wave 게이트**")
    st.caption("W00의 어려운 본편 컷·연결 컷마다 source/sequence/artifact "
               "해시와 durable job 결과, 선정 이유, 검토자와 "
               "KEEP/CHANGE/MIX 판단을 기록합니다. 합성 fixture 검토자는 "
               "SYNTHETIC_FIXTURE로 표시되고 실제 W00/작품 승인을 만들지 "
               "않습니다.")
    gate = _guarded(lambda: gate_status(p))
    if gate is None:
        return
    rows = [{"대상": row["target_id"], "기록": row.get("pilot_id") or "—",
             "상태": STATE_KO.get(row["state"], row["state"]),
             "결정": row.get("decision") or "—",
             "검토자": row.get("reviewer") or "—",
             "검토자 종류": ("합성 fixture" if row["synthetic"]
                            else "사람" if row["synthetic"] is False
                            else "—")}
            for row in gate["pilots"]]
    if rows:
        st.dataframe(rows, hide_index=True)
    else:
        st.caption("W00 파일럿 대상이 없습니다 — 먼저 제작 순서를 선언하세요.")
    if gate["gate"]["state"] == "OPEN":
        st.success("다음 wave 게이트 OPEN — 열린 범위: "
                   + ", ".join(gate["open_waves"] or ["—"]))
    else:
        st.warning("다음 wave 게이트 BLOCKED")
        for reason in gate["gate"]["reasons"]:
            st.caption("· " + reason)
    if gate["unknown"]:
        st.warning("미확정(UNKNOWN 등) durable 작업이 게이트를 막습니다: "
                   + ", ".join(f"{j['job_id']}={j['status']}"
                               for j in gate["unknown"]))
    if gate["issues"]:
        st.dataframe([{"출처": i["source"], "대상": i["target"],
                       "처리": i["disposition"], "내용": i["note"],
                       "이유": i.get("reason") or "—",
                       "범위": str(i["scope"]) if i.get("scope") is not None
                       else "—"}
                      for i in gate["issues"]], hide_index=True)
    if gate["quote"]["totals"]:
        st.caption("견적 합계: " + ", ".join(
            f"{v} {k}" for k, v in gate["quote"]["totals"].items()))
    if gate["usage"]["reserved_totals"]:
        st.caption("예약(지출) 합계: " + ", ".join(
            f"{v} {k}" for k, v in gate["usage"]["reserved_totals"].items()))

    targets = gate["pilot_targets"]
    target_ids = targets["cuts"] + targets["transitions"]
    if not target_ids:
        return
    pick = st.selectbox("파일럿 대상", target_ids, key="an_pilot_target")
    decision = st.radio("경로 판단", ["KEEP", "CHANGE", "MIX"],
                        horizontal=True, key="an_pilot_decision")
    c1, c2 = st.columns(2)
    reviewer = c1.text_input("파일럿 검토자", key="an_pilot_reviewer")
    kind = c2.selectbox("검토자 종류", ["SYNTHETIC_FIXTURE", "HUMAN"],
                        key="an_pilot_kind",
                        help="HUMAN은 박준태 또는 기록된 위임자만 가능합니다")
    reason = st.text_input("선정·검토 이유", key="an_pilot_reason")
    c3, c4 = st.columns(2)
    cap_ref = c3.text_input("capability 참조 (없으면 비움)", key="an_pilot_cap")
    job_ref = c4.text_input("durable job 결과 id (없으면 비움)",
                            key="an_pilot_job")
    c5, c6 = st.columns(2)
    issue_kind = c5.selectbox(
        "발견한 문제 처리", ["없음", "FIX_REQUIRED", "INTENTIONAL",
                       "ACCEPTED_LIMITATION"], key="an_pilot_issue",
        help="INTENTIONAL/ACCEPTED_LIMITATION은 이유와 범위가 필요합니다")
    issue_note = c6.text_input("문제 내용", key="an_pilot_issue_note")
    issue_reason = c5.text_input("처리 이유", key="an_pilot_issue_reason")
    issue_scope = c6.text_input("범위 (라벨)", key="an_pilot_issue_scope")

    def pilot_issues():
        if issue_kind == "없음":
            return []
        item = {"disposition": issue_kind, "note": issue_note}
        if issue_reason.strip():
            item["reason"] = issue_reason.strip()
        if issue_scope.strip():
            item["scope"] = issue_scope.strip()
        return [item]

    if st.button("파일럿 기록", key="an_pilot_record"):
        _do(lambda: record_pilot(
            p, pick, decision=decision, reviewer=reviewer,
            reviewer_kind=kind, reason=reason,
            capability_ref=cap_ref.strip() or None,
            job_result_id=job_ref.strip() or None,
            issues=pilot_issues()),
            f"{pick}의 W00 파일럿을 기록했습니다")

    with st.expander("승인자 위임 · 추가 지출 승인", expanded=False):
        st.caption("HUMAN 승인자는 박준태이거나 아래 위임 기록이 있어야 "
                   "합니다. CHANGE/MIX 경로의 추가 지출은 결정 revision에 "
                   "묶인 별도 승인이 필요합니다.")
        c1, c2 = st.columns(2)
        dg_delegate = c1.text_input("위임받는 승인자", key="an_dg_delegate")
        dg_scope = c2.multiselect("위임 범위", list(DELEGATION_SCOPES),
                                  default=["W00_PILOT"], key="an_dg_scope")
        dg_days = c1.number_input("위임 유효 일수", min_value=1, value=7,
                                  key="an_dg_days")
        if st.button("위임 기록", key="an_dg_record"):
            _do(lambda: record_delegation(
                p, delegate=dg_delegate, scope=list(dg_scope),
                expires_at_ms=int(time.time() * 1000)
                + int(dg_days) * 86400000),
                f"{dg_delegate}에게 {dg_scope} 위임을 기록했습니다")
        sa_approver = st.text_input("지출 승인자", key="an_sa_approver")
        sa_kind = st.selectbox("지출 승인자 종류",
                               ["SYNTHETIC_FIXTURE", "HUMAN"],
                               key="an_sa_kind")
        sa_jobs = st.multiselect(
            "승인 대상 작업", [o["job_id"] for o in
                             gate["spend"]["obligations"]],
            key="an_sa_jobs")
        sa_reason = st.text_input("추가 지출 승인 근거", key="an_sa_reason")
        if st.button("추가 지출 승인 기록", key="an_sa_record"):
            _do(lambda: record_spend_approval(
                p, approver=sa_approver, reviewer_kind=sa_kind,
                reason=sa_reason, jobs=list(sa_jobs)),
                "추가 지출 승인을 기록했습니다")


def _deliveries(p, pick):
    """Separate sealed clean/subbed delivery approvals (ANIM-022)."""
    st.markdown("**sealed 전달 파일 별도 승인 — clean / subbed**")
    st.caption("clean과 subbed는 각자의 파일 해시에 묶여 별도로 승인됩니다 — "
               "한쪽 승인이 다른 쪽을 대신하지 않고, 승인은 봉인된 build를 "
               "바꾸지 않습니다. 승인자는 박준태 또는 기록된 위임자이며 "
               "합성 fixture 승인은 SYNTHETIC_FIXTURE로만 기록됩니다.")
    delivery = _guarded(lambda: deliverable_status(p, pick["build_id"]))
    if delivery is None:
        return
    st.dataframe([{"파일": name, "상태": STATE_KO.get(row["state"],
                                                     row["state"]),
                   "승인": row["approval_id"] or "—",
                   "승인자": row["approver"] or "—",
                   "종류": row["reviewer_kind"] or "—"}
                  for name, row in delivery["deliverables"].items()],
                 hide_index=True)
    c1, c2 = st.columns(2)
    deliverable = c1.selectbox("전달 파일", list(DELIVERABLES),
                               key="an_del_pick")
    kind = c2.selectbox("승인자 종류", ["SYNTHETIC_FIXTURE", "HUMAN"],
                        key="an_del_kind")
    approver = st.text_input("전달 파일 승인자", key="an_del_approver")
    if st.button("전달 파일 승인 기록", key="an_del_ok"):
        _do(lambda: approve_delivery(p, pick["build_id"], deliverable,
                                     approver=approver,
                                     reviewer_kind=kind),
            f"{pick['build_id']}의 {deliverable} 승인을 기록했습니다")


def _output(p, entries, locks, route, reviews):
    waves_doc = _waves(p, entries)
    _locks(p, waves_doc)
    _route(p, waves_doc, route)
    _w00_gate(p)
    st.markdown("**Final 후보와 최종 승인**")
    if st.button("Final 후보 만들기 (compile-final)", key="an_final_make"):
        with st.spinner("Final 후보를 봉인하고 있습니다…"):
            result = _guarded(lambda: compile_final_candidate(p))
        if result is not None:
            st.success(f"{result['build_id']} · FINAL_CANDIDATE 봉인 완료")
            st.video(result["output"])
    builds = list_builds(p)
    if not builds:
        st.caption("아직 봉인된 빌드가 없습니다.")
        from app.delivery_package import render_delivery
        render_delivery(p, None)
        return
    pick = st.selectbox(
        "빌드", builds, key="an_final_build",
        format_func=lambda b: f"{b['build_id']} · "
        f"{b.get('mode') or b.get('document_type') or 'Build 1'} · "
        f"{b.get('status')}")
    film = _guarded(lambda: film_review_status(p, pick["build_id"]))
    if film is not None:
        st.caption(f"{pick['build_id']}의 FINAL_FILM 검수: "
                   f"{STATE_KO.get(film['state'], film['state'])}")
    reviewer = st.text_input("최종 검토자", key="an_film_reviewer")
    st.caption("최종 승인은 정확한 빌드와 현재 검수에 묶이는 protocol 기록"
               "입니다 — 실제 작품 승인·qualification·release를 대신하지 "
               "않습니다.")
    c1, c2 = st.columns(2)
    if c1.button("최종 승인 기록 (approve-film)", key="an_final_ok"):
        _do(lambda: _approve_film(p, pick, reviewer, "APPROVED"),
            f"{pick['build_id']} 최종 승인을 기록했습니다")
    c1.caption("protocol 검토 기록 전용 — governed 전달 승인(박준태 또는 "
               "기록된 위임자)은 아래 전달 파일별 승인에서 진행합니다.")
    if c2.button("최종 수정 필요 기록", key="an_final_fix"):
        _do(lambda: _approve_film(p, pick, reviewer, "FIX_REQUIRED"),
            f"{pick['build_id']} 수정 필요를 기록했습니다")
    if pick.get("document_type") == "animation_build" \
            and pick.get("status") == "COMPLETE":
        _deliveries(p, pick)
    st.divider()
    from app.diagnostics_ui import render_diagnostics
    render_diagnostics(p, pick["build_id"])
    st.divider()
    from app.delivery_package import render_delivery
    render_delivery(p, pick["build_id"])


def _b_flash(action, result):
    """Flash a path-B job result; ambiguous/in-flight outcomes are warnings."""
    status = result.get("status") or result.get("state")
    detail = result.get("detail") or ""
    if status in {"UNKNOWN", "CANCEL_REQUESTED"}:
        st.session_state["an_warn"] = (
            f"{action}: {result['job_id']}은(는) {status} — 같은 작업의 "
            "'상태 확인'만 허용됩니다. 새 제출·대체 제출·자동 재시도는 "
            "차단됩니다. " + detail)
    elif status == "FAILED_CONFIRMED":
        st.session_state["an_warn"] = (
            f"{action}: {result['job_id']} 실패가 확인됐습니다 — "
            f"{(result.get('failure') or {}).get('reason', '')} " + detail)
    else:
        st.session_state["an_flash"] = f"{action}: {status} " + detail
    st.session_state["an_detail"] = result
    st.rerun()


def _b_do(action, fn):
    result = _guarded(fn)
    if result is not None:
        _b_flash(action, result)
    return result


def _path_b(p, shot_id):
    """Path-B segment generation against the dev/test fake adapter only.

    fake_segment is a local deterministic double (FAKE/UNQUALIFIED); real
    providers stay declaration-only and refuse submission. The UI drives
    control inputs → capability preflight → quote → separate cost approval
    → reservation+submit → explicit same-identity reconcile → returned-clip
    verification → draft import → DRAFT commit, and surfaces UNKNOWN/cancel
    fencing instead of retrying or polling.
    """
    st.divider()
    st.markdown("**07 · 경로 B — 구간 생성 adapter**")
    st.caption("개발·시험용 fake adapter (UNQUALIFIED) `fake_segment`만 "
               "연결합니다. 실제 provider 호출·유료 생성·credential은 "
               "없습니다 (선언만 된 adapter는 제출이 거부됩니다).")
    if not safe_path(p, plan_path(shot_id)).is_file():
        st.caption("01 · 준비에서 이 컷의 샷 계획을 먼저 저장하세요.")
        return
    plan = _guarded(lambda: load_shot_plan(p, shot_id))
    if plan is None:
        return
    segments = [s for s in plan["segments"] if s["path"] == "B"]
    if not segments:
        st.caption("이 컷의 계획에는 경로 B 구간이 없습니다.")
        return

    st.markdown("**① 제어 입력 — 이 컷의 B 구간만 쓰는 조건 이미지**")
    ctrl = st.file_uploader("조건 이미지 (PNG)", type=["png"],
                            key="an_b_ctrl_file")
    c1, c2 = st.columns(2)
    ctrl_role = c1.selectbox("역할", ["keypose", "breakdown", "pose", "layout"],
                             key="an_b_ctrl_role")
    ctrl_frame = c2.number_input("컷 기준 프레임", min_value=0, value=0,
                                 key="an_b_ctrl_frame")
    if st.button("제어 입력 기록", key="an_b_ctrl_import",
                 disabled=not ctrl):
        def run_ctrl():
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / _upload_name(ctrl)
                source.write_bytes(ctrl.getvalue())
                return import_segment_control(p, shot_id, source,
                                              role=ctrl_role,
                                              frame=int(ctrl_frame))
        _do(run_ctrl, f"{shot_id} 프레임 {int(ctrl_frame)} · {ctrl_role} "
                      "제어 입력을 DRAFT로 기록했습니다")

    st.markdown("**② 구간 · capability 검사 · 견적**")
    options = {f"[{s['start']}, {s['end']}) · {', '.join(s['capabilities'])}":
               s for s in segments}
    pick = st.selectbox("B 구간", list(options), key="an_b_segment")
    seg = options[pick]
    caps = _guarded(lambda: make_adapter(p, "fake_segment").capabilities())
    if caps:
        st.caption(f"adapter {caps['adapter_id']} · {caps['provider_class']} · "
                   f"{caps['qualification_state']} · native "
                   f"{caps['native_width']}x{caps['native_height']}@"
                   f"{caps['native_fps']}fps")
    if st.button("capability 검사 + 견적", key="an_b_quote"):
        result = _guarded(lambda: segment_quote(p, shot_id, seg["start"],
                                                seg["end"]))
        if result is not None:
            st.session_state["an_b_quote_state"] = {
                "shot_id": shot_id,
                "segment": [seg["start"], seg["end"]], "result": result}
            st.session_state["an_flash"] = (
                f"견적 {result['amount']} {result['unit']} — 이 견적과 "
                "별도의 비용 승인이 필요합니다")
            st.session_state["an_detail"] = result
            st.rerun()
    qstate = st.session_state.get("an_b_quote_state")
    live_quote = qstate if qstate and qstate["shot_id"] == shot_id else None
    if live_quote:
        quote = live_quote["result"]
        if live_quote["segment"] != [seg["start"], seg["end"]]:
            st.warning("선택한 구간과 묶인 견적이 다릅니다 — 이 구간으로 "
                       "다시 견적을 받으세요.")
        cols = st.columns(3)
        cols[0].metric("견적", f"{quote['amount']} {quote['unit']}")
        cols[1].metric("job", quote["job_id"])
        cols[2].metric("quote", quote["quote_id"][:12] + "…")

    st.markdown("**③ 비용 승인 → 예약 → 제출** — 견적과 별도의 사람 승인")
    c1, c2 = st.columns(2)
    approver = c1.text_input("견적 승인자", key="an_b_approver")
    cap = c2.number_input("승인 상한 (크레딧)", min_value=0, value=0,
                          key="an_b_cap")
    consent = st.checkbox("위 견적의 fake 크레딧 비용을 승인합니다",
                          key="an_b_approve")
    can_submit = bool(live_quote and consent and approver.strip())
    if st.button("승인한 견적으로 예약·제출", key="an_b_submit",
                 disabled=not can_submit):
        _b_do("제출", lambda: segment_submit(
            p, shot_id, seg["start"], seg["end"], approver=approver.strip(),
            quote_id=live_quote["result"]["quote_id"], cap=int(cap)))

    jobs = (_guarded(lambda: segment_jobs(p, shot_id)) or {}).get("jobs", [])
    if not jobs:
        st.caption("이 컷의 B 작업 기록이 아직 없습니다.")
        return
    st.markdown("**④ 작업 상태** — 같은 identity의 명시적 확인만 (자동 "
                "polling 없음)")
    st.dataframe([{"job": j["job_id"],
                   "구간": f"[{j['segment']['start']}, {j['segment']['end']})",
                   "상태": j["status"], "attempt": j["attempt"],
                   "과금": j["charge_state"], "adapter": j["adapter"]}
                  for j in jobs], hide_index=True)
    job_pick = st.selectbox("작업", [j["job_id"] for j in jobs],
                            key="an_b_job")
    c1, c2 = st.columns(2)
    if c1.button("상태 확인 (reconcile)", key="an_b_reconcile"):
        _b_do("상태 확인", lambda: segment_reconcile(p, job_pick))
    if c2.button("취소 요청", key="an_b_cancel"):
        _b_do("취소", lambda: segment_cancel(p, job_pick))

    st.markdown("**⑤ 결과 검증 → draft 가져오기 → 컷 commit**")
    c1, c2 = st.columns(2)
    src_start = c1.number_input("사용 시작 소스 프레임", min_value=0, value=0,
                                key="an_b_src_start")
    src_count = c2.number_input("사용 프레임 수 (0 = 자동)", min_value=0,
                                value=0, key="an_b_src_count")
    c1, c2 = st.columns(2)
    if c1.button("반환 클립 검증 + draft 가져오기", key="an_b_import"):
        _b_do("가져오기", lambda: segment_import(
            p, job_pick, source_start=int(src_start),
            source_count=int(src_count) or None))
    if c2.button("검증된 B 구간으로 컷 commit", key="an_b_commit"):
        _b_do("commit", lambda: commit_segment_sequence(p, shot_id))

    job_record = next((j for j in jobs if j["job_id"] == job_pick), None)
    if job_record:
        with st.expander("job journal — 입력 digest · adapter 버전 · "
                         "동작/관측 기록", expanded=False):
            st.caption(
                f"adapter {job_record['adapter']} "
                f"({caps['adapter_id'] if caps else 'fake_segment_v1'}) · "
                f"request {job_record['request_id']} · operation "
                f"{job_record['operation_id']} · "
                f"{job_record['provider_class']}/"
                f"{job_record['qualification_state']}")
            records = _guarded(lambda: load_job_journal(p, shot_id,
                                                        job_pick)) or []
            if records:
                st.dataframe(
                    [{"at": r["at"], "event": r["event"],
                      "attempt": r["attempt_id"], "request": r["request_id"]}
                     for r in records], hide_index=True)
                st.caption("레코드 상세 (source/input digest·toolchain·견적·"
                           "관측 결과·실행 동작)")
                st.json(_jsonable(records))
            else:
                st.caption("journal 기록이 아직 없습니다.")


# ---------------------------------------------------------------------------


def render_animation(p):
    """The FRAME_ANIMATION_V1 section; caller gates on production_profile."""
    p = Path(p)
    st.subheader("FRAME_ANIMATION_V1 · 프레임 애니메이션")
    st.caption("한 컷의 준비 → 동작·노출 → import → 검토 → 수정 → 출력·승인 "
               "흐름입니다. 가져온 그림은 모두 DRAFT이고, 이 화면의 기록은 "
               "검수 protocol이지 실제 작품 승인이 아닙니다 "
               "(qualification UNQUALIFIED · acceptance PENDING · "
               "release NOT_AUTHORIZED).")
    flash = st.session_state.pop("an_flash", None)
    if flash:
        st.success(flash)
    warn = st.session_state.pop("an_warn", None)
    if warn:
        st.warning(warn)
    detail = st.session_state.pop("an_detail", None)
    if detail is not None:
        with st.expander("기록 상세 (해시 포함)", expanded=False):
            st.json(_jsonable(detail))
    try:
        timeline = load_animation_timeline(p)
        locks = lock_status(p)
        route = route_status(p)
        reviews = review_status(p, strict=False)
    except FilmError as exc:
        st.error(str(exc))
        return
    entries = timeline["entries"]
    cols = st.columns(4)
    cols[0].metric("PLAN", locks["plan"]["state"])
    wave_current = sum(1 for s in locks["waves"].values()
                       if s["state"] == "CURRENT")
    cols[1].metric("WAVE", f"{wave_current}/{len(locks['waves'])}"
                   if locks["waves"] else "미선언")
    cols[2].metric("경로 결정", route["checkpoint"])
    cols[3].metric("FINAL", locks["final"]["state"])
    current = sum(1 for r in reviews["targets"].values()
                  if r["state"] == "CURRENT")
    st.caption(f"검수 현재 {current}/{len(reviews['targets'])} · "
               f"qualification UNQUALIFIED · acceptance PENDING · "
               f"release NOT_AUTHORIZED")
    with st.expander("상태 상세 (해시·바인딩)", expanded=False):
        st.json(_jsonable({"locks": locks, "route": route,
                           "reviews": reviews}))
    by_instance = {e["instance_id"]: e for e in entries}
    pick = st.selectbox(
        "작업할 컷", [e["instance_id"] for e in entries], key="an_shot",
        format_func=lambda i: f"{i} · {by_instance[i]['shot_id']} · "
        f"{by_instance[i]['used_source_range'][1] - by_instance[i]['used_source_range'][0]}프레임")
    entry = by_instance[pick]
    shot_id = entry["shot_id"]
    pin = (_registry(p).get("assignments") or {}).get(shot_id)
    if pin:
        st.caption(f"{shot_id} 채택 시퀀스: {pin['asset_id']} "
                   f"r{pin['revision']} (DRAFT)")
    else:
        st.caption(f"{shot_id}: 아직 채택된 시퀀스가 없습니다")
    with st.expander("01 · 준비 — 자산·계획·패킷·프레임 검산", expanded=True):
        _prepare(p, shot_id)
    with st.expander("02 · 동작·노출 — keypose와 교체 그림", expanded=False):
        _motion(p, shot_id)
    with st.expander("03 · Import — 시퀀스 / draft 프레임", expanded=True):
        _import(p, shot_id)
    with st.expander("04 · 검토 — Preview 재생과 현재 검수", expanded=True):
        _review(p, shot_id, entry, entries, reviews)
    with st.expander("05 · 수정 — 한 컷 교체와 재검수 범위", expanded=True):
        _fix(p, shot_id, locks, reviews)
    with st.expander("06 · 출력·승인 — LOCK·경로 결정·최종 승인",
                     expanded=True):
        _output(p, entries, locks, route, reviews)
    _path_b(p, shot_id)
