"""Drive archive · bounded cache · restore section (ANIM-013).

A real Google OAuth client does not exist for this program, so the connect
button runs the fake consent flow against an on-disk fake Drive; every
surface says the real connection stays UNQUALIFIED. Tokens live in the
in-memory session only — nothing in this panel writes a token, code or
client secret to disk, logs or the project.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import streamlit as st

from engine.core import FilmError, read
from engine import settings
from engine.drive_archive import (archive_frames, archive_status,
                                  restore_archive)
from engine.drive_oauth import (FakeOAuthFlow, MemoryTokenStore,
                                connect_drive, load_connection_metadata)
from engine.storage_backends.fake_drive import FakeDriveBackend
from engine.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_BYTES = 256 * 1024 * 1024
SESSION_KEY = "drive_session"
BACKEND_KEY = "drive_backend"


def _home():
    """User-level drive home (fake-drive objects, cache, grant metadata)."""
    return settings.home() / "drive"


def _backend():
    backend = st.session_state.get(BACKEND_KEY)
    if backend is None:
        backend = FakeDriveBackend(root=_home() / "fake-drive",
                                   provider_checksum=None)
        st.session_state[BACKEND_KEY] = backend
    return backend


def _guarded(fn):
    try:
        return fn()
    except FilmError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.error(f"처리하지 못했습니다: {exc}")
    return None


def _manifest_dir():
    path = _home() / "archives"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_manifest(doc):
    import json
    path = _manifest_dir() / f"{doc['archive_id']}.json"
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def _load_manifest(archive_id):
    return read(_manifest_dir() / f"{archive_id}.json")


def _connect():
    session = st.session_state.get(SESSION_KEY)
    meta = load_connection_metadata(_home())
    st.markdown("**Google Drive 연결**")
    st.caption("요청 범위는 drive.file(앱이 만든 파일 + 직접 고른 파일)뿐입니다. "
               "실제 OAuth client가 아직 없어 이 버튼은 fake 동의 화면을 씁니다. "
               "실제 Drive 연결 qualification: **UNQUALIFIED**")
    if session is None:
        if meta:
            st.info(f"이전 연결 기록이 있습니다 (epoch {meta['credential_epoch']}). "
                    "토큰은 저장되지 않으므로 다시 연결해야 합니다.")
        if st.button("Google 계정으로 연결 (fake)", key="arc_connect"):
            result = _guarded(lambda: connect_drive(
                FakeOAuthFlow(), MemoryTokenStore(), _backend(),
                home=_home()))
            if result is not None:
                st.session_state[SESSION_KEY] = result
                st.rerun()
    else:
        m = session.metadata()
        st.success(f"연결됨 · {m['connection_id']} · "
                   f"binding {m['account_binding_digest'][:12]}… · "
                   f"epoch {m['credential_epoch']} · "
                   f"토큰 저장: {m['token_store']}")
        if st.button("연결 해제 (로그아웃)", key="arc_logout"):
            session.logout()
            st.session_state[SESSION_KEY] = None
            st.rerun()
    return session


def _archive(session):
    st.markdown("**아카이브 만들기**")
    folder = st.text_input("PNG 폴더 (FAV1 pack으로 보관)", key="arc_source")
    if not folder.strip() or session is None:
        if session is None:
            st.caption("먼저 Drive에 연결하세요.")
        return
    if st.button("아카이브 만들기", key="arc_make"):
        def run():
            source = Path(folder).expanduser().resolve()
            if not source.is_dir():
                raise FilmError(f"폴더가 없습니다: {source}")
            pngs = sorted(source.glob("*.png"))
            if not pngs:
                raise FilmError("PNG 멤버가 없습니다")
            members = [{"member_id": f.stem, "frame_index": i,
                        "data": f.read_bytes()}
                       for i, f in enumerate(pngs)]
            return archive_frames(
                session.authorized, members, profile="DRIVE_BOUNDED",
                connection={k: session.metadata()[k] for k in
                            ("connection_id", "account_binding_digest",
                             "credential_epoch")},
                min_level="FULL_READBACK")
        result = _guarded(run)
        if result is not None:
            path = _save_manifest(result["archive"])
            st.success(f"아카이브 봉인 · level {result['achieved_level']} · "
                       f"manifest {result['manifest_object_id']}")
            st.caption(f"매니페스트 저장: {path} · "
                       f"sha {result['archive_sha256'][:16]}…")


def _restore(session):
    st.markdown("**복원 / 캐시 상태**")
    manifests = sorted(f.stem for f in _manifest_dir().glob("*.json"))
    if not manifests:
        st.caption("저장된 아카이브가 없습니다.")
        return
    pick = st.selectbox("아카이브", manifests, key="arc_pick")
    dest = st.text_input("복원할 폴더", key="arc_dest")
    cache_cap = st.number_input("캐시 상한 (bytes)", min_value=1 << 20,
                                value=DEFAULT_CACHE_BYTES, key="arc_cap")
    workspace = Workspace(_home() / "cache", int(cache_cap))
    cols = st.columns(3)
    cols[0].metric("캐시 사용", f"{workspace.used_bytes:,} B")
    cols[1].metric("캐시 여유", f"{workspace.available_bytes:,} B")
    cols[2].metric("캐시 항목", len(workspace.entries))
    if st.button("선택 아카이브 상태", key="arc_status"):
        doc = _guarded(lambda: _load_manifest(pick))
        if doc:
            st.json(_guarded(lambda: archive_status(
                session.authorized if session else _backend(), doc)))
    if session is not None and dest.strip() \
            and st.button("아카이브 복원", key="arc_restore"):
        def run():
            doc = _load_manifest(pick)
            target = Path(dest).expanduser().resolve()
            return restore_archive(session.authorized, doc, workspace,
                                   target, allowed_root=target.parent,
                                   whole_pack_cap=int(cache_cap))
        result = _guarded(run)
        if result is not None:
            st.success(f"복원 완료 · {len(result['restored'])}개 멤버 · "
                       f"{result['verification']}")
            st.caption(" · ".join(result["restored"][:12]))


def render_archive(project):
    """The Drive archive section for the connection tab."""
    st.divider()
    st.subheader("Drive 아카이브 · 복원 (fake backend)")
    st.caption("pack/member 해시·범위 읽기·bounded cache·staging 복원만 검사합니다. "
               "fake 성공은 실제 Drive qualification이 아닙니다.")
    session = _connect()
    _archive(session)
    _restore(session)
