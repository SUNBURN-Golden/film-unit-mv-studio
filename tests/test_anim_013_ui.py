"""ANIM-013 browser-UI fixture: the Drive archive section in the control
panel — fake connect, archive, restore and cache status via AppTest.

The fake Drive lives under FILM_UNIT_HOME/drive; no real OAuth, network or
credentials are involved, and the panel keeps reporting UNQUALIFIED.
"""
import json
import shutil
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from anim_013_kit import make_png

from engine.audio import analyze, synth_test_audio
from engine.core import init_project
from engine.production import make_package

ROOT = Path(__file__).resolve().parents[1]
NAME = "zz_anim013_ui"


@pytest.fixture
def screen(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    root = ROOT / "projects"
    root.mkdir(exist_ok=True)
    audio = synth_test_audio(tmp_path / "t.wav", seconds=6)
    project = init_project(root, NAME, audio, "UI fixture", synthetic=True,
                           aspect="16:9")
    analyze(project)
    make_package(project)
    try:
        at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                               default_timeout=90).run()
        at.sidebar.selectbox[0].select(NAME).run()
        assert not at.exception
        yield at, tmp_path
    finally:
        shutil.rmtree(project, ignore_errors=True)


def test_archive_section_reports_fake_and_unqualified(screen):
    at, _ = screen
    assert any("UNQUALIFIED" in c.value for c in at.caption)
    assert any("fake" in s.value.lower() for s in at.subheader)


def test_connect_archive_restore_and_cache_status(screen, tmp_path):
    at, home = screen
    connect_btn = next(b for b in at.button if b.key == "arc_connect")
    connect_btn.click().run()
    assert not at.exception
    assert any("연결됨" in s.value for s in at.success)
    # Only non-secret grant metadata was persisted under the user home.
    meta = json.loads((home / "home/drive/drive_connection.json")
                      .read_text())
    assert set(meta) == {"connection_id", "account_binding_digest",
                         "credential_epoch", "provider", "scopes",
                         "token_store"}
    for key in ("access_token", "refresh_token", "authorization_code",
                "client_secret"):
        assert key not in json.dumps(meta)
    source = tmp_path / "frames"
    source.mkdir()
    for i in range(2):
        (source / f"f{i}.png").write_bytes(make_png(i, 16, 16))
    field = next(t for t in at.text_input if t.key == "arc_source")
    field.set_value(str(source))
    at.run()                       # the button appears once a folder is set
    next(b for b in at.button if b.key == "arc_make").click().run()
    assert not at.exception
    assert any("아카이브 봉인" in s.value for s in at.success)
    # The restore boundary is the fixed drive home under FILM_UNIT_HOME.
    dest = home / "home" / "drive" / "restored"
    next(t for t in at.text_input if t.key == "arc_dest") \
        .set_value(str(dest))
    at.run()
    next(b for b in at.button if b.key == "arc_restore").click().run()
    assert not at.exception
    assert any("복원 완료" in s.value for s in at.success)
    assert sorted(p.name for p in dest.glob("*.png")) \
        == ["F_000001.png", "F_000002.png"]
    next(b for b in at.button if b.key == "arc_status").click().run()
    assert not at.exception
    assert (dest / "F_000001.png").read_bytes() == make_png(0, 16, 16)


def test_restore_refuses_a_symlinked_destination(screen, tmp_path):
    at, home = screen
    next(b for b in at.button if b.key == "arc_connect").click().run()
    source = tmp_path / "frames"
    source.mkdir()
    (source / "f0.png").write_bytes(make_png(0, 16, 16))
    next(t for t in at.text_input if t.key == "arc_source") \
        .set_value(str(source))
    at.run()
    next(b for b in at.button if b.key == "arc_make").click().run()
    assert not at.exception
    # A symlink inside the allowed root pointing outside it is refused —
    # the panel must not resolve it away before the restore gate runs.
    real = tmp_path / "outside"
    real.mkdir()
    link = home / "home" / "drive" / "link"
    link.symlink_to(real)
    next(t for t in at.text_input if t.key == "arc_dest") \
        .set_value(str(link))
    at.run()
    next(b for b in at.button if b.key == "arc_restore").click().run()
    assert not at.exception
    assert any("RESTORE_PATH_REJECTED" in e.value for e in at.error)
    assert list(real.iterdir()) == []


def test_logout_disconnects_the_session(screen):
    at, _ = screen
    next(b for b in at.button if b.key == "arc_connect").click().run()
    next(b for b in at.button if b.key == "arc_logout").click().run()
    assert not at.exception
    assert any("연결 (fake)" in b.label or "연결" in b.label
               for b in at.button if b.key == "arc_connect")
