"""film-delivery-package panel via Streamlit AppTest.

AppTest drives the real control-panel widgets (buttons and captions). It
does not see a live spinner frame or a desktop keyboard focus ring; those
stay unverified without a browser.
"""
import shutil
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_compiler_v03 import fixture_project  # noqa: E402
from test_film_delivery_package import _seal  # noqa: E402

from engine.animation_migrate import animation_init  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NAME = "compiler_fixture"
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="ffmpeg is not installed")


def _open(box, project_name=NAME):
    at = AppTest.from_file(str(ROOT / "app/control_panel.py"),
                           default_timeout=120).run()
    at.sidebar.selectbox[0].select(project_name).run()
    assert not at.exception
    return at


def _captions(at):
    return [c.value for c in at.caption]


def _button(at, key):
    return next(b for b in at.button if b.key == key)


def _keys(at):
    return [b.key for b in at.button if b.key]


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FILM_UNIT_PROJECTS", str(tmp_path / "projects"))
    return tmp_path


def test_legacy_screen_has_no_delivery_panel(box):
    projects = box / "projects"
    projects.mkdir()
    fixture_project(projects, seconds=2, shot_count=1)
    at = _open(box)
    assert not any("전달 묶음" in value for value in _captions(at))
    assert not any(key.startswith("dp_") for key in _keys(at))


def test_empty_then_states_error_and_keyboard(box):
    projects = box / "projects"
    projects.mkdir()
    project = fixture_project(projects, seconds=2, shot_count=1)
    animation_init(project)
    at = _open(box)
    assert any("전달 묶음: 봉인된 빌드가 없습니다." in value
               for value in _captions(at))
    assert not any(key.startswith("dp_make_") for key in _keys(at))

    _seal(project, "B0001", mode="PREVIEW", draft=True,
          candidate="FINAL_CANDIDATE_READY")
    (project / "builds" / "B0001" / "Final.mp4").write_bytes(b"Final")
    at = _open(box)
    captions = _captions(at)
    assert any("미리보기: 미리보기" in value for value in captions)
    assert any("Final 후보: Final 후보 아님" in value for value in captions)
    assert any("감독 채택: 감독 미채택" in value for value in captions)
    assert any("외부 공개: NOT_AUTHORIZED" in value for value in captions)
    assert any("진행: 대기" in value for value in captions)
    assert any("파일 이름에 Final이 있어도" in value for value in captions)
    make = _button(at, "dp_make_B0001")
    verify = _button(at, "dp_verify_B0001")
    assert make.disabled is False
    assert verify.disabled is False

    make.click().run()
    assert not at.exception
    assert any("묶음을 만들었습니다" in s.value for s in at.success)
    assert any("NOT_AUTHORIZED" in s.value for s in at.success)
    assert (project / "delivery" / "B0001" / "bundle.json").is_file()

    at = _open(box)
    assert any("진행: 묶음이 있습니다" in value for value in _captions(at))
    _button(at, "dp_make_B0001").click().run()
    assert any("already exists" in e.value or "이미" in e.value
               for e in at.error)

    at = _open(box)
    _button(at, "dp_verify_B0001").click().run()
    assert any("manifest와 같습니다" in s.value for s in at.success)

    broken = project / "delivery" / "B0001" / "media" / "thumbnail.png"
    broken.write_bytes(broken.read_bytes() + b"\x00")
    at = _open(box)
    _button(at, "dp_verify_B0001").click().run()
    assert any("무결성 실패" in e.value for e in at.error)
