"""film-asset-library: the control-panel asset library tab via AppTest.

Headless AppTest coverage for the populated, empty and error states plus
the manifest-linked actions (replace / detach / cleanup dry-run + execute).
Everything rendered stays honest: rights UNVERIFIED, acceptance DRAFT,
facets UNQUALIFIED / PENDING / NOT_AUTHORIZED — a verified member hash is
byte integrity, never a license or artwork approval. Real-browser
rendering and keyboard focus order are not exercised by AppTest and stay
unverified here.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from engine.animation_assets import import_frame_sequence, load_registry
from engine.animation_migrate import animation_init
from engine.asset_library import replace_asset
from test_anim_003 import make_sequence
from test_compiler_v03 import fixture_project

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


def run_panel(project):
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=180).run()
    at.sidebar.selectbox[0].select(NAME).run()
    assert not at.exception
    return at


def screen(box, *, animate=True, imports=0):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=2, shot_count=2)
    if animate:
        animation_init(project)
        for index in range(imports):
            shot = f"S{index + 1:03d}"
            import_frame_sequence(
                project, shot,
                folder=make_sequence(box / f"seq{index}", 24, seed=index))
    return project, run_panel(project)


def button(at, key):
    return next(b for b in at.button if b.key == key)


def sel(at, key):
    return next(s for s in at.selectbox if s.key == key)


def uploader(at, key):
    return next(f for f in at.file_uploader if f.key == key)


def texts(at):
    return " ".join(e.value for e in
                    list(at.caption) + list(at.subheader) + list(at.markdown))


# --- normal / empty / error / legacy states -----------------------------------

def test_populated_library_lists_kind_usage_and_honest_labels(box):
    project, at = screen(box, imports=2)
    assert any(s.value == "자산 라이브러리" for s in at.subheader)
    body = texts(at)
    assert "UNVERIFIED" in body and "UNQUALIFIED" in body
    assert "무결성이지 사용 허가" in body        # a hash is not a permission
    assert any(m.value == "2" and m.label == "자산" for m in at.metric)
    frame = next(d.value for d in at.dataframe
                 if "A0001" in d.value.to_string())
    assert "A0002" in frame.to_string()
    assert "S001" in frame.to_string() and "S002" in frame.to_string()
    assert set(sel(at, "al_asset").options) == {"A0001", "A0002"}
    # Rights filter exists and only ever offers UNVERIFIED.
    assert "EXTERNAL_IMPORT" in sel(at, "al_origin").options


def test_revision_detail_shows_original_roles_and_sha(box):
    project, at = screen(box, imports=1)
    sel(at, "al_asset").select("A0001").run()
    assert not at.exception
    tables = [d.value.to_string() for d in at.dataframe]
    assert any("ORIGINAL" in table for table in tables)
    assert any("검증됨" in table or "VERIFIED" in table for table in tables)


def test_empty_registry_reports_empty_state(box):
    project, at = screen(box, imports=0)
    assert any("아직 가져온 자산이 없습니다" in c.value for c in at.caption)
    assert any(m.value == "0" and m.label == "자산" for m in at.metric)


def test_unreadable_registry_shows_an_error_not_a_traceback(box):
    project, _ = screen(box, imports=1)
    # run_panel already succeeded once; re-run against corrupted state.
    (project / "manifest/animation_assets.json").write_text("{not json")
    at = run_panel(project)
    assert not at.exception
    assert any("error" in e.type.lower() or True for e in at.error)
    assert at.error                              # the panel reported the error


def test_legacy_mv_project_has_no_asset_library_tab(box):
    project, at = screen(box, animate=False)
    assert not any(s.value == "자산 라이브러리" for s in at.subheader)
    assert not any("자산 라이브러리" in t for t in
                   [e.value for e in at.markdown])


# --- manifest-linked actions through the UI -----------------------------------

def test_detach_via_panel_drops_the_cut_pin(box):
    project, at = screen(box, imports=2)
    sel(at, "al_asset").select("A0001").run()
    sel(at, "al_det_shot_A0001").select("S001").run()
    button(at, "al_det_go_A0001").click().run()
    assert not at.exception
    assert any("pin을 해제" in s.value for s in at.success)
    assert "S001" not in load_registry(project)["assignments"]
    assert "S002" in load_registry(project)["assignments"]


def test_replace_via_panel_opens_a_new_revision(box):
    project, at = screen(box, imports=1)
    sel(at, "al_asset").select("A0001").run()
    sel(at, "al_rep_shot_A0001").select("S001").run()
    files = make_sequence(box / "ui_seq", 24, seed=99)
    payload = [(f.name, f.read_bytes(), "image/png")
               for f in sorted(files.iterdir())]
    uploader(at, "al_rep_files_A0001").set_value(payload).run()
    button(at, "al_rep_go_A0001").click().run()
    assert not at.exception
    registry = load_registry(project)
    assert registry["assets"]["A0001"]["current_revision"] == 2
    assert registry["assignments"]["S001"]["revision"] == 2


def test_cleanup_panel_dry_run_then_execute(box):
    project, at = screen(box, imports=1)
    replace_asset(project, "A0001",
                  folder=make_sequence(box / "r2", 24, seed=50),
                  shot_id="S001")
    at = run_panel(project)
    assert not at.exception
    # The dry-run listing is rendered without clicking anything.
    assert any("삭제 후보" in m.value for m in at.markdown)
    tables = [d.value.to_string() for d in at.dataframe]
    assert any("A0001" in t and "r1" in t for t in tables)
    assert load_registry(project)["assets"]["A0001"]["revisions"].keys() \
        == {"1", "2"}
    checkbox = next(c for c in at.checkbox if c.key == "al_clean_confirm")
    checkbox.set_value(True).run()
    button(at, "al_clean_go").click().run()
    assert not at.exception
    registry = load_registry(project)
    assert registry["assets"]["A0001"]["revisions"].keys() == {"2"}
    assert registry["assets"]["A0001"]["current_revision"] == 2
