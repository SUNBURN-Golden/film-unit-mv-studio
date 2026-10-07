"""film-asset-library: the "09 · 자산" panel of the control panel.

Renders the asset library read model (engine/asset_library.py) for
FRAME_ANIMATION_V1 projects only — control_panel gates the tab on the
production profile, so LEGACY_MV projects render exactly as before. Every
imported asset stays DRAFT, the rights column is always UNVERIFIED and a
verified member hash is shown as byte integrity — never as a license,
permission or artwork approval. Member images drawn here are the original
member bytes scaled for display — a display scaling, never an approval.

Actions call the engine directly: replace opens a new revision, detach
drops a cut's assignment pin, and cleanup runs its dry-run listing on
every render and only deletes still-unprotected revisions on the explicit
execute button. Facets stay UNQUALIFIED / PENDING / NOT_AUTHORIZED.
"""
from pathlib import Path
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from app.animation_ui import _guarded, _jsonable, _upload_name
from engine.asset_library import (cleanup_unused, detach_asset,
                                  library_view, replace_asset)
from engine.core import FilmError, safe_path

KIND_KO = {"FRAME_SEQUENCE": "프레임 시퀀스", "COMPOSITE_SEQUENCE": "합성 시퀀스",
           "LAYER_RGBA": "RGBA 레이어", "MASK": "마스크",
           "REPLACEMENT_DRAWING": "교체 그림", "RIG_SPEC": "리그",
           "CONTROL_IMAGE": "제어 이미지"}
STATUS_KO = {"VERIFIED": "검증됨", "MISSING": "누락", "CORRUPT": "손상",
             "LENGTH_MISMATCH": "길이 불일치", "HASH_MISMATCH": "해시 불일치",
             "UNRECORDED": "기록 없음"}


def _do(fn, message):
    """Run a mutating engine action; flash it on this panel and redraw."""
    result = _guarded(fn)
    if result is not None:
        st.session_state["al_flash"] = message
        st.session_state["al_detail"] = result
        st.rerun()
    return result


def _filters(view):
    c1, c2, c3 = st.columns(3)
    kinds = sorted({r["kind"] for a in view["assets"] for r in a["revisions"]})
    kind = c1.selectbox("종류", ["(전체)"] + kinds, key="al_kind",
                        format_func=lambda k: KIND_KO.get(k, k))
    origins = sorted({r["origin"] for a in view["assets"]
                      for r in a["revisions"] if r["origin"]})
    origin = c2.selectbox("출처", ["(전체)"] + origins, key="al_origin")
    shots = sorted({u["shot_id"] for a in view["assets"]
                    for r in a["revisions"] for u in r["used_in"]
                    if u.get("shot_id")})
    shot = c3.selectbox("사용 컷", ["(전체)"] + shots, key="al_shot")
    return (None if kind == "(전체)" else kind,
            None if origin == "(전체)" else origin,
            None if shot == "(전체)" else shot)


def _table(view):
    rows = []
    for asset in view["assets"]:
        shots = sorted({u["shot_id"] for r in asset["revisions"]
                        for u in r["used_in"] if u.get("shot_id")})
        rows.append({"자산": asset["asset_id"],
                     "종류": KIND_KO.get(asset["kind"], asset["kind"]),
                     "현재": f"r{asset['current_revision']}",
                     "리비전": asset["revisions_count"],
                     "승인": asset["acceptance"],
                     "권리": asset["rights"]["state"],
                     "사용 컷": ", ".join(shots) or "—",
                     "빌드": ", ".join(asset["builds"]) or "—",
                     "무결성": STATUS_KO.get(asset["integrity"],
                                           asset["integrity"])})
    st.dataframe(rows, hide_index=True)


def _detail(p, view):
    pick = st.selectbox("자산", [a["asset_id"] for a in view["assets"]],
                        key="al_asset")
    asset = next(a for a in view["assets"] if a["asset_id"] == pick)
    labels = [f"r{r['revision']}" + (" · 현재" if r["current"] else "")
              for r in asset["revisions"]]
    index = st.selectbox("리비전", range(len(asset["revisions"])),
                         format_func=lambda i: labels[i],
                         key=f"al_rev_{pick}_{asset['revisions_count']}")
    revision = asset["revisions"][index]
    st.caption(
        f"{revision['kind']} · 출처 {revision['origin']} · 승인 "
        f"{revision['acceptance']} · 준비 {revision['preparation']} · 권리 "
        f"{revision['rights']['state']} · 무결성 "
        f"{STATUS_KO.get(revision['integrity'], revision['integrity'])}")
    st.code(f"{pick}:{revision['revision']}:{revision['content_sha256']}",
            language=None)
    prov = revision["provenance"]
    with st.expander("출처와 pin", expanded=False):
        st.json(_jsonable(prov))
    if revision["used_in"]:
        st.markdown("**사용처**")
        st.dataframe([{"컷": u.get("shot_id") or "—",
                       "인스턴스": u.get("instance_id") or "—",
                       "어디서": u.get("where") or u.get("reason") or "—"}
                      for u in revision["used_in"]], hide_index=True)
    if revision["protected"]:
        with st.expander("삭제를 막는 참조", expanded=False):
            for reason in revision["held_by"]:
                st.caption("· " + reason)
    st.markdown("**멤버 파일** — 기록된 멤버가 실제 원본(ORIGINAL)입니다")
    st.dataframe(
        [{"파일": f["relative_name"], "역할": f["role"],
          "상태": STATUS_KO.get(f["status"], f["status"]),
          "바이트": f["byte_length"],
          "sha256": (f["sha256"] or "—")[:16]}
         for f in revision["files"]], hide_index=True)
    original = next((f for f in revision["files"]
                     if f["role"] == "ORIGINAL"
                     and f["status"] == "VERIFIED"
                     and f["relative_name"].endswith(".png")), None)
    if original is not None:
        st.image(str(safe_path(p, original["relative_name"])), width=200,
                 caption="원본 멤버 바이트를 표시용으로 축소한 것입니다 — "
                         "별도 프록시 단계는 아직 없고 승인 기록이 아닙니다")
    if revision["not_verified"]:
        st.caption("미확인 항목: " + ", ".join(revision["not_verified"]))
    return asset, revision


def _replace(p, asset):
    kind = asset["kind"]
    if kind == "COMPOSITE_SEQUENCE":
        st.caption("합성 시퀀스는 해당 컷의 compile-shot으로만 새 리비전이 "
                   "만들어집니다.")
        return
    if kind == "FRAME_SEQUENCE":
        shots = sorted({u["shot_id"] for r in asset["revisions"]
                        for u in r["used_in"]
                        if u.get("where") == "current manifest"
                        and u.get("shot_id")})
        shot = st.selectbox("계속 배정할 컷", shots, key=f"al_rep_shot_{asset['asset_id']}")
        files = st.file_uploader("새 프레임 PNG 묶음 (업로드 순서=프레임 순서)",
                                 type=["png"], accept_multiple_files=True,
                                 key=f"al_rep_files_{asset['asset_id']}")
        if st.button("시퀀스 교체 (새 리비전)",
                     key=f"al_rep_go_{asset['asset_id']}",
                     disabled=not files or not shot):
            def run():
                with tempfile.TemporaryDirectory() as tmp:
                    folder = Path(tmp)
                    for position, uploaded in enumerate(files):
                        (folder / f"f{position:04d}.png").write_bytes(
                            uploaded.getvalue())
                    return replace_asset(p, asset["asset_id"], folder=folder,
                                         shot_id=shot)
            _do(run, f"{asset['asset_id']}를 새 리비전으로 교체했습니다 — "
                     "묶여 있던 검수·LOCK은 재검수가 필요합니다")
        return
    accept = ["json"] if kind == "RIG_SPEC" else ["png"]
    upload = st.file_uploader(
        f"교체 파일 ({'RIG_SPEC JSON' if kind == 'RIG_SPEC' else 'PNG'})",
        type=accept, key=f"al_rep_file_{asset['asset_id']}")
    if st.button("새 리비전으로 교체", key=f"al_rep_go_{asset['asset_id']}",
                 disabled=upload is None):
        def run():
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / _upload_name(upload)
                source.write_bytes(upload.getvalue())
                if kind == "RIG_SPEC":
                    return replace_asset(
                        p, asset["asset_id"],
                        spec=json.loads(source.read_text(encoding="utf-8")))
                return replace_asset(p, asset["asset_id"], file=source)
        _do(run, f"{asset['asset_id']}를 새 리비전으로 교체했습니다")


def _detach(p, asset):
    shots = sorted({u["shot_id"] for r in asset["revisions"]
                    for u in r["used_in"]
                    if u.get("where") == "current manifest"
                    and u.get("shot_id")})
    if not shots:
        st.caption("현재 manifest에서 이 자산을 쓰는 컷이 없습니다.")
        return
    shot = st.selectbox("분리할 컷", shots, key=f"al_det_shot_{asset['asset_id']}")
    st.caption("분리하면 그 컷은 시퀀스가 해결되지 않는 상태가 되고, 묶여 "
               "있던 검수·LOCK은 낡아집니다. 자산 기록과 바이트는 남습니다.")
    if st.button(f"{shot}에서 분리", key=f"al_det_go_{asset['asset_id']}"):
        _do(lambda: detach_asset(p, shot),
            f"{shot}의 배정 pin을 해제했습니다 — 컷은 미해결 상태입니다")


def _cleanup(p):
    result = _guarded(lambda: cleanup_unused(p))
    if result is None:
        return
    st.caption("현재 manifest·빌드·LOCK·검수·패킷·다른 자산이 참조하지 않는 "
               "리비전만 삭제 후보입니다. 기본은 dry-run 목록입니다.")
    if result["candidates"]:
        st.markdown("**삭제 후보 (dry-run)**")
        st.dataframe([{"자산": c["asset_id"], "리비전": f"r{c['revision']}",
                       "종류": c["kind"], "멤버": c["members"],
                       "바이트": c["bytes"]}
                      for c in result["candidates"]], hide_index=True)
    else:
        st.caption("삭제 가능한 미사용 리비전이 없습니다.")
    if result["protected"]:
        with st.expander(f"참조로 보호된 리비전 {len(result['protected'])}개",
                         expanded=False):
            st.dataframe(
                [{"자산": r["asset_id"], "리비전": f"r{r['revision']}",
                  "보호 근거": "; ".join(r["reasons"])}
                 for r in result["protected"]], hide_index=True)
    confirm = st.checkbox("dry-run 후보의 registry 기록과 멤버 파일을 실제로 "
                          "삭제합니다", key="al_clean_confirm")
    if st.button("미사용 리비전 삭제 실행", key="al_clean_go",
                 disabled=not confirm or not result["candidates"]):
        executed = _guarded(lambda: cleanup_unused(p, execute=True))
        if executed is not None:
            st.session_state["al_flash"] = (
                f"{len(executed['deleted'])}개 리비전, "
                f"{executed['files_removed']}개 파일을 삭제했습니다")
            st.session_state["al_detail"] = executed
            st.rerun()


def render_asset_library(p):
    """The asset library panel; caller gates on production_profile."""
    p = Path(p)
    st.subheader("자산 라이브러리")
    st.caption("어떤 이미지가 어느 컷·빌드·검수에 쓰였는지 찾습니다. 모든 "
               "자산은 DRAFT이고 권리 확인은 UNVERIFIED입니다 — member "
               "hash 일치는 바이트 무결성이지 사용 허가·저작권 승인이 "
               "아닙니다 (qualification UNQUALIFIED · acceptance PENDING · "
               "release NOT_AUTHORIZED).")
    flash = st.session_state.pop("al_flash", None)
    if flash:
        st.success(flash)
    detail = st.session_state.pop("al_detail", None)
    if detail is not None:
        with st.expander("기록 상세 (해시 포함)", expanded=False):
            st.json(_jsonable(detail))
    try:
        base = library_view(p)
    except FilmError as exc:
        st.error(str(exc))
        return
    summary = base["summary"]
    cols = st.columns(5)
    cols[0].metric("자산", summary["assets"])
    cols[1].metric("리비전", summary["revisions"])
    cols[2].metric("배정된 컷", summary["assigned"])
    cols[3].metric("미참조", summary["unprotected"])
    cols[4].metric("누락·손상", summary["missing"] + summary["corrupt"])
    for warning in base["warnings"]:
        st.warning(warning)
    for collision in base["name_collisions"]:
        st.warning(
            f"같은 파일명 '{collision['source_name']}'을 서로 다른 자산이 "
            "다른 내용으로 갖고 있습니다 — 파일명이 아니라 (자산, 리비전, "
            "content hash)로 구분되는 별개 콘텐츠입니다: " + ", ".join(
                f"{h['asset_id']} r{h['revision']}"
                for h in collision["holders"]))
    if not base["assets"]:
        st.caption("아직 가져온 자산이 없습니다 — 08 · ANIMATION 탭의 "
                   "import로 등록됩니다.")
    else:
        kind, origin, shot = _filters(base)
        view = _guarded(lambda: library_view(
            p, kind=kind, origin=origin, shot_id=shot))
        if view is not None:
            _table(view)
            if not view["assets"]:
                st.caption("이 조건에 맞는 자산이 없습니다.")
            else:
                asset, _revision = _detail(p, view)
                with st.expander("교체 — 새 리비전 (교체는 삭제가 아닙니다)",
                                 expanded=False):
                    _replace(p, asset)
                with st.expander("분리 — 컷의 배정 pin 해제", expanded=False):
                    _detach(p, asset)
    with st.expander("미사용 자산 정리 (dry-run이 기본)", expanded=False):
        _cleanup(p)
