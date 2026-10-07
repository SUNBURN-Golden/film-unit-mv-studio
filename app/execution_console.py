"""film-execution-console — 실행 작업실 queue/status panel.

A thin read surface over `engine/execution_console.ExecutionConsole`.
Everything shown is derived from the durable journal / Coordinator /
UploadTracker / ArchiveCommitter / committed perf state — the panel is
never a second source of truth, does not poll and does not auto-retry.

- CANCEL_REQUESTED is shown as a request, never as a confirmed stop.
- UNKNOWN fences every action except an explicit status query on the
  existing request identity — no duplicate remote submit is ever offered.
- UPLOADED (bytes/offset confirmed) and VERIFIED (readback/hash matched)
  are separate, side-by-side indicators.
- Only verified checkpoints are listed as reusable artifacts.

All workers/backends reachable here are local fakes — every line is
UNQUALIFIED, never service qualification.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError
from engine.execution_console import STAGES, ExecutionConsole

_ACTION_LABEL = {
    "status_query": "상태 조회 — reconcile",
    "cancel": "취소 요청",
    "verify": "출력 검증 실행",
    "seal": "저널 봉인",
    "new_attempt": "새 attempt (사용자 확인 필요)",
    "release_reservation": "예약 해제",
    "reconcile_upload": "업로드 대조 — reconcile",
    "resume_upload": "업로드 이어하기",
    "reconcile_commit": "seal 대조 — reconcile",
    "new_intent": "새 업로드 intent",
}

_STAGE_KO = {"compose": "compose 합성", "encode": "encode 인코딩",
             "mux": "mux 먹싱", "verify": "verify 검증",
             "upload": "upload 업로드", "seal": "seal 봉인"}

_KIND_KO = {"job": "작업", "upload": "업로드", "commit": "archive commit",
            "pipeline": "파이프라인"}


def _unmet(entry, item, console):
    """Explain why an allowed action stays disabled in this session."""
    if not entry["allowed"]:
        return entry["reason"]
    for need in entry.get("needs", ()):
        if need == "worker" and item.get("worker_id") not in \
                console.workers:
            return (f"worker {item.get('worker_id')}가 이 UI 세션에 "
                    "연결되어 있지 않습니다 — 실행 중이던 프로세스나 CLI에서 "
                    "처리합니다")
        if need == "backend" and item.get("source") not in \
                console.backends:
            return ("archive backend가 이 UI 세션에 연결되어 있지 않습니다 "
                    "— CLI reconcile 경로를 사용합니다")
        if need == "data":
            return ("이어받을 객체 바이트가 필요합니다 — CLI/실행 세션에서 "
                    "같은 intent를 이어갑니다")
        if need in ("plan", "user_continued"):
            return ("새 attempt는 사용자의 명시적 continue와 동일한 plan "
                    "재바인딩이 필요합니다 — CLI resume 경로")
    return None


def _render_item(console, item):
    label = item.get("label") or item["ref"]
    kind = _KIND_KO.get(item["kind"], item["kind"])
    st.markdown(f"**{label}** · {kind} · **{item['state']}**")
    rows = []
    for stage in STAGES:
        row = item["stages"][stage]
        rows.append({"stage": _STAGE_KO[stage],
                     "status": row["status"],
                     "detail": row.get("detail") or ""})
    st.dataframe(rows, use_container_width=True, hide_index=True)
    ind = item["indicators"]
    net = ind["network"]
    remote = ind["remote"]
    verify = ind.get("upload_verify") or {}
    remote_txt = remote.get("last_report") or \
        ("해당 없음" if remote.get("basis") == "inapplicable" else "기록 없음")
    if remote.get("detail"):
        remote_txt = f"{remote_txt} — {remote['detail']}"
    verify_txt = verify.get("level") or \
        ", ".join(verify.get("levels") or []) or \
        ("확인됨" if verify.get("verified") else "없음/미완료")
    st.caption(f"네트워크 **{net['state']}**"
               + (f" ({', '.join(net['lost'])})" if net.get("lost") else "")
               + f" · 원격 **{remote_txt}** ({remote['basis']})"
               + f" · 업로드 검증 **{verify_txt}**")
    return item


def _render_explain(console, item):
    """Stop point, reusable verified artifacts, recovery buttons."""
    try:
        detail = console.explain(item["item_id"])
    except FilmError as exc:
        st.error(str(exc))
        return
    stop = detail["stop_point"]
    st.markdown(f"중단 위치: **{stop['stage']}** · `{stop['state']}`")
    if stop.get("detail"):
        st.caption(stop["detail"])
    cov = detail.get("coverage") or (detail.get("stop_point") or {}).get(
        "coverage")
    if cov:
        st.caption(f"수령된 coverage: {cov['covered']} · "
                   f"미완료: {cov['missing']}")
    reusable = detail["reusable_artifacts"]
    if reusable:
        st.markdown("재사용 가능한 artifact (검증된 checkpoint만):")
        rows = []
        for a in reusable:
            rows.append({
                "type": a["type"],
                "sha256": (a.get("sha256") or "")[:16],
                "level": a.get("level") or a.get("basis") or "",
                "scope": str(a.get("covered") or a.get("object_id") or "")})
        st.dataframe(rows, use_container_width=True, hide_index=True)
        for a in reusable:
            if a.get("members"):
                st.caption(f"member digests: `{a['members']}`")
    else:
        st.caption("재사용 가능한 검증 artifact가 없습니다 — 수령 영수증은 "
                   "checkpoint가 아니며 재사용되지 않습니다.")
    na = detail["new_attempt"]
    verdict = ("필요" if na["required"] else
               "불명" if na["required"] is None else "불필요")
    st.markdown(f"새 attempt 필요: **{verdict}** · "
                f"허용: **{'O' if na['allowed'] else 'X'}**")
    st.caption(na["reason"])
    for note in detail.get("notes", ()):
        st.info(note)
    st.caption("복귀 동작 — 버튼은 Tab 포커스 후 Enter로 실행할 수 "
               "있습니다. 거부된 동작은 사유만 표시합니다.")
    for entry in detail.get("recovery", ()):
        action = entry["action"]
        block = _unmet(entry, item, console)
        label = _ACTION_LABEL.get(action, action)
        key = f"exec:{action}:{item['item_id']}"
        if st.button(label, key=key, disabled=bool(block)):
            try:
                out = console.run_action(item["item_id"], action)
            except FilmError as exc:
                st.error(str(exc))
            else:
                if out.get("result") == "OK":
                    st.session_state["exec_toast"] = \
                        f"{label} — {out.get('state') or '완료'}"
                    st.rerun()
                st.warning(f"{label} — 거부: {out.get('reason')}")
        if block:
            st.caption(f"{label}: {block}")


def render_execution_console(project=None, *, state_dir=None,
                             coordinator=None, journal_dirs=None,
                             perf_roots=None, workers=None,
                             backends=None):
    """The execution queue/status section for the connection tab."""
    st.divider()
    st.subheader("실행 작업실 · execution queue")
    toast = st.session_state.pop("exec_toast", None)
    if toast:
        st.success(toast)
    st.caption("durable journal·Coordinator·업로드 tracker에서 읽은 파생 "
               "상태입니다 — 새 진실 원천이 아니며, 폴링·자동 재시도는 "
               "없습니다. fake/local 실행은 UNQUALIFIED입니다.")
    if state_dir is None:
        default = (Path(project) / "render" / "execution") if project \
            else None
        if default is None:
            st.info("프로젝트를 선택하면 실행 상태를 표시합니다.")
            return
        name = Path(project).name if project else "none"
        state_dir = st.text_input(
            "실행 상태 폴더 (coordinator state dir)",
            str(default), key=f"exec_console_dir_{name}")
    root = Path(state_dir)
    if not root.is_dir():
        st.info("실행 상태 폴더가 없습니다 — 작업이 계획·제출되면 "
                "queue/status가 여기에 표시됩니다.")
        return
    try:
        console = ExecutionConsole(
            coordinator=coordinator, state_dir=root,
            journal_dirs=journal_dirs or (), perf_roots=perf_roots or (),
            workers=workers or {}, backends=backends or {})
        report = console.status()
    except FilmError as exc:
        st.error(f"실행 상태를 읽지 못했습니다: {exc}")
        return
    except Exception as exc:  # corrupt journal surfaces as its own error
        st.error(f"실행 상태를 읽지 못했습니다: {exc}")
        return
    for j in report["journals"]:
        if not j.get("journal") and j.get("records") is None:
            continue
        if j.get("journal") is None and not j.get("records"):
            st.caption(f"journal {j['dir']}: 없음")
            continue
        line = (f"journal `{j['dir']}` — {j['records']} records · "
                f"tail **{j['tail']}**")
        if j.get("fenced") or j.get("tail") != "CLEAN":
            line += f" · {j.get('tail_reason') or ''}"
        if j.get("replay_error"):
            line += f" · replay 오류: {j['replay_error']}"
        st.caption(line)
    if report["fenced"]:
        st.error("저널이 fence 상태입니다 — 새 부수효과는 거부되며 명시적 "
                 "reconcile이 먼저 필요합니다.")
    if report["unresolved"]:
        st.warning("미해결: " + " · ".join(
            f"{u['item_id']} → {u['needs']}" for u in report["unresolved"]))
    items = report["items"]
    if not items:
        st.info("실행 작업이 없습니다 — 실행 queue가 비어 있습니다.")
        return
    st.caption(f"{len(items)}개 실행 항목 · qualification "
               f"**{report['qualification_state']}**")
    for item in items:
        _render_item(console, item)
        detail_needed = item["in_flight"] or item["closed"] or (
            item["kind"] == "job" and
            (item["cancel"]["requested"] or
             (item["state"] == "VERIFIED" and not item["sealed"]))) \
            or item["kind"] in ("upload", "commit")
        if detail_needed:
            with st.expander("중단 위치 · 재사용 artifact · 복귀",
                             expanded=bool(item["in_flight"])):
                _render_explain(console, item)
    st.caption("qualification: **UNQUALIFIED** — fake/local 실행은 실제 "
               "서비스 승인이 아닙니다. 업로드 완료(UPLOADED)와 검증"
               "(VERIFIED)은 별개의 사실로 표시됩니다.")
