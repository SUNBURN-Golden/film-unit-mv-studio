"""Cross-platform regressions for packaging boundaries, not renderer behavior."""
import hashlib
import io
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import zipfile

import pytest

from desktop import ffmpeg_setup, launcher
from desktop.ffmpeg_setup import extract_checked
from desktop.runtime import configure_runtime, data_root, resource_root
from engine.core import FilmError, atomic_text, project_mutex, read, write


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """configure_runtime() edits os.environ directly; that must never leak into other tests."""
    monkeypatch.setattr(os, "environ", os.environ.copy())


def test_utf8_bytes_and_korean_windows_paths(tmp_path):
    path = tmp_path / "한글 ' 프로젝트" / "가사.json"
    write(path, {"lyrics": "한 곡에서 한 편으로 🎵"})
    assert read(path)["lyrics"] == "한 곡에서 한 편으로 🎵"
    assert b"\r\n" not in path.read_bytes()
    assert "한 곡" in path.read_bytes().decode("utf-8")


def test_mutex_excludes_another_process_and_releases_after_crash(tmp_path):
    script = "from engine.core import project_mutex; import sys,time;\nwith project_mutex(sys.argv[1]):\n print('LOCKED',flush=True)\n time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        with pytest.raises(FilmError, match="already rendering"):
            with project_mutex(tmp_path):
                pytest.fail("Second writer acquired project lock")
    finally:
        process.kill()
        process.communicate(timeout=10)
    with project_mutex(tmp_path):
        atomic_text(tmp_path / "released.txt", "ok")


def test_user_data_never_uses_bundle_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "내 영상"))
    monkeypatch.delenv("FILM_UNIT_PROJECTS", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "immutable-bundle"), raising=False)
    root = configure_runtime()
    assert root == data_root() == tmp_path / "내 영상"
    assert resource_root() == tmp_path / "immutable-bundle"
    assert Path(os.environ["FILM_UNIT_PROJECTS"]).is_dir()
    assert not resource_root().exists()


def test_ffmpeg_checksum_and_fixed_path_extraction(tmp_path):
    archive = tmp_path / "runtime.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("runtime/bin/ffmpeg.exe", b"test-ffmpeg")
        z.writestr("runtime/bin/ffprobe.exe", b"test-ffprobe")
        z.writestr("../../outside.exe", b"must never extract")
    with pytest.raises(RuntimeError, match="checksum"):
        extract_checked(archive, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    extract_checked(archive, tmp_path / "runtime", expected_sha=checksum)
    assert (tmp_path / "runtime/bin/ffmpeg.exe").read_bytes() == b"test-ffmpeg"
    assert sorted(p.name for p in (tmp_path / "runtime").iterdir()) == ["bin", "download.json"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects require Windows")
def test_windows_job_stops_server_and_its_encoder_children():
    import ctypes
    from ctypes import wintypes
    from desktop.windows_job import Job
    # Console Python is deliberate here; the windowed EXE has its own GUI smoke.
    script = "import sys,subprocess,time;sys.stdin.readline();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(p.pid,flush=True);time.sleep(60)"
    parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    job = Job()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        job.assign(parent)
        parent.stdin.write("START\n")
        parent.stdin.flush()
        pid = int(parent.stdout.readline())
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        assert handle
        job.close()
        parent.wait(timeout=5)
        assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        job.close()
        if parent.poll() is None:
            parent.kill()
        parent.communicate(timeout=5)
        if handle:
            kernel.CloseHandle(handle)


# ---- FFmpeg download table and installer ------------------------------------------------

HEX64 = re.compile(r"[0-9a-f]{64}")


def test_every_supported_platform_has_pinned_downloads():
    assert set(ffmpeg_setup.SOURCES) == {"windows-amd64", "macos-amd64", "macos-arm64", "linux-amd64", "linux-arm64"}
    for key, entry in ffmpeg_setup.SOURCES.items():
        if "archive" in entry:
            assert entry["archive"]["url"].startswith("https://") and ffmpeg_setup.VERSION in entry["archive"]["url"]
            assert HEX64.fullmatch(entry["archive"]["sha256"])
            continue
        os_name, arch = key.split("-")
        assert set(entry["files"]) == {"ffmpeg", "ffprobe"}
        for name, (url, checksum) in entry["files"].items():
            assert url.startswith("https://ffmpeg.martin-riedl.de/download/")
            assert f"/{os_name}/{arch}/" in url and url.endswith(f"_{ffmpeg_setup.VERSION}/{name}.zip")
            assert HEX64.fullmatch(checksum)
    all_hashes = [c for e in ffmpeg_setup.SOURCES.values() for c in
                  ([e["archive"]["sha256"]] if "archive" in e else [c for _, c in e["files"].values()])]
    assert len(all_hashes) == len(set(all_hashes))


@pytest.mark.parametrize("platform_name, machine, expected", [
    ("win32", "AMD64", "windows-amd64"), ("darwin", "arm64", "macos-arm64"), ("darwin", "x86_64", "macos-amd64"),
    ("linux", "x86_64", "linux-amd64"), ("linux", "aarch64", "linux-arm64")])
def test_platform_key_names_the_running_machine(monkeypatch, platform_name, machine, expected):
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert ffmpeg_setup.platform_key() == expected
    assert ffmpeg_setup.binary_names() == (("ffmpeg.exe", "ffprobe.exe") if platform_name == "win32" else ("ffmpeg", "ffprobe"))


def one_file_zip(path, name, content=b"binary"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, content)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_single_binary_archive_is_checked_then_made_executable(tmp_path):
    archive = tmp_path / "ffmpeg.zip"
    checksum = one_file_zip(archive, "ffmpeg", b"test-ffmpeg")
    with pytest.raises(RuntimeError, match="checksum"):
        ffmpeg_setup.extract_single(archive, tmp_path / "bad", "ffmpeg", "0" * 64)
    assert not (tmp_path / "bad").exists()
    assert ffmpeg_setup.extract_single(archive, tmp_path / "ok", "ffmpeg", checksum) == checksum
    target = tmp_path / "ok/bin/ffmpeg"
    assert target.read_bytes() == b"test-ffmpeg"
    if os.name != "nt":
        assert os.access(target, os.X_OK)


def test_single_binary_archive_with_another_name_or_path_is_refused(tmp_path):
    for wrong in ("ffprobe", "../ffmpeg", "sub/ffmpeg"):
        archive = tmp_path / "x.zip"
        checksum = one_file_zip(archive, wrong)
        with pytest.raises(RuntimeError, match="Unexpected"):
            ffmpeg_setup.extract_single(archive, tmp_path / "out", "ffmpeg", checksum)
    assert not list(tmp_path.glob("out/**/*"))


@pytest.fixture
def fake_source(monkeypatch):
    """Two tiny zips instead of real downloads; counts how many downloads happen."""
    payload = {}
    files = {}
    for name in ("ffmpeg", "ffprobe"):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(name, b"fake-" + name.encode())
        payload[name] = archive.getvalue()
        files[name] = (f"https://example.invalid/{name}.zip", hashlib.sha256(payload[name]).hexdigest())
    calls = []

    def download(url, path, progress):
        calls.append(url)
        Path(path).write_bytes(payload[url.rsplit("/", 1)[1].removesuffix(".zip")])

    monkeypatch.setattr(ffmpeg_setup, "source", lambda: {"provider": "test", "files": files})
    monkeypatch.setattr(ffmpeg_setup, "binary_names", lambda: ("ffmpeg", "ffprobe"))
    monkeypatch.setattr(ffmpeg_setup, "_download", download)
    return calls, files


def test_ffmpeg_is_installed_once_with_a_record_of_where_it_came_from(tmp_path, fake_source):
    calls, files = fake_source
    messages = []
    final = ffmpeg_setup.ensure_ffmpeg(tmp_path, messages.append)
    assert final == ffmpeg_setup.install_dir(tmp_path)
    assert sorted(p.name for p in (final / "bin").iterdir()) == ["ffmpeg", "ffprobe"]
    record = read(final / "download.json")
    assert record["provider"] == "test" and record["files"]["ffmpeg"]["sha256"] == files["ffmpeg"][1]
    assert ffmpeg_setup.ensure_ffmpeg(tmp_path) == final and len(calls) == 2     # nothing downloaded again
    assert any("무결성" in m for m in messages)


def test_a_failed_or_tampered_download_installs_nothing(tmp_path, fake_source, monkeypatch):
    calls, files = fake_source
    monkeypatch.setattr(ffmpeg_setup, "source", lambda: {"provider": "test", "files": {
        "ffmpeg": (files["ffmpeg"][0], "0" * 64), "ffprobe": files["ffprobe"]}})
    with pytest.raises(RuntimeError, match="checksum"):
        ffmpeg_setup.ensure_ffmpeg(tmp_path)
    assert not ffmpeg_setup.install_dir(tmp_path).exists()
    assert list((tmp_path / "runtime").iterdir()) == []          # the staging folder is removed too


class FakeResponse:
    def __init__(self, data, length=None):
        self.stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_names_itself_and_notices_a_cut_off_connection(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ffmpeg_setup.urllib.request, "urlopen", lambda request, timeout: seen.append(request) or FakeResponse(b"x" * 10))
    ffmpeg_setup._download("https://example.invalid/a.zip", tmp_path / "a.zip", lambda m: None)
    assert seen[0].get_header("User-agent") == ffmpeg_setup.USER_AGENT and "Python-urllib" not in ffmpeg_setup.USER_AGENT
    monkeypatch.setattr(ffmpeg_setup.urllib.request, "urlopen", lambda request, timeout: FakeResponse(b"x" * 10, length=100))
    with pytest.raises(RuntimeError, match="cut off"):
        ffmpeg_setup._download("https://example.invalid/a.zip", tmp_path / "b.zip", lambda m: None)


# ---- per-OS locations and launcher behaviour --------------------------------------------

def test_each_os_keeps_user_data_in_its_own_conventional_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("FILM_UNIT_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert data_root() == tmp_path / "local/FILM_UNIT"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert data_root() == tmp_path / "home/Library/Application Support/FILM_UNIT"
    monkeypatch.setattr(sys, "platform", "linux")
    assert data_root() == tmp_path / "home/.local/share/FILM_UNIT"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert data_root() == tmp_path / "xdg/FILM_UNIT"


def test_runtime_shares_one_home_for_projects_keys_and_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FILM_UNIT_PROJECTS", raising=False)
    (ffmpeg_setup.install_dir(tmp_path / "home") / "bin").mkdir(parents=True)
    configure_runtime()
    assert os.environ["FILM_UNIT_HOME"] == str(tmp_path / "home")
    assert Path(os.environ["FILM_UNIT_PROJECTS"]) == tmp_path / "home/projects"
    assert os.environ["PATH"].startswith(str(ffmpeg_setup.install_dir(tmp_path / "home") / "bin"))
    from engine import settings
    assert settings.home() == tmp_path / "home"          # the model picker stores keys here


@pytest.mark.parametrize("platform_name, command", [("darwin", ["open"]), ("linux", ["xdg-open"]), ("win32", None)])
def test_folder_button_uses_the_os_file_manager(monkeypatch, tmp_path, platform_name, command):
    monkeypatch.setattr(sys, "platform", platform_name)
    result = launcher.folder_command(tmp_path)
    assert (result[:-1] if result else result) == command
    if result:
        assert result[-1] == str(tmp_path)


def test_folder_button_falls_back_to_the_browser_when_no_file_manager_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    opened = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("xdg-open")))
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    launcher.open_folder(tmp_path)
    assert opened == [tmp_path.as_uri()]


def test_launcher_starts_without_a_window_when_asked_or_when_tk_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(launcher, "headless", lambda root: 7)
    assert launcher.main(["--no-gui"]) == 7
    monkeypatch.setitem(sys.modules, "tkinter", None)           # makes "import tkinter" fail
    assert launcher.main([]) == 7
    with pytest.raises(RuntimeError, match="no Tk"):
        launcher.main(["--smoke-gui", str(tmp_path / "gui.json")])


def test_launcher_starts_without_a_window_when_there_is_no_display(monkeypatch, tmp_path):
    tkinter = pytest.importorskip("tkinter")
    monkeypatch.setenv("FILM_UNIT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(launcher, "headless", lambda root: 9)
    monkeypatch.setattr(launcher, "gui", lambda root, report=None: (_ for _ in ()).throw(tkinter.TclError("no display")))
    assert launcher.main([]) == 9
    with pytest.raises(tkinter.TclError):                        # the window test must not pass by falling back
        launcher.main(["--smoke-gui", str(tmp_path / "gui.json")])


# ---- release files -----------------------------------------------------------------------

from scripts import frozen_check, package_desktop as pack  # noqa: E402


@pytest.mark.parametrize("platform_name, machine, suffix, expected", [
    ("win32", "AMD64", ".zip", "FILM_UNIT-Windows-x64.zip"), ("darwin", "arm64", ".zip", "FILM_UNIT-macOS-arm64.zip"),
    ("linux", "x86_64", ".tar.gz", "FILM_UNIT-Linux-x64.tar.gz"), ("linux", "aarch64", ".tar.gz", "FILM_UNIT-Linux-arm64.tar.gz")])
def test_release_files_are_named_for_their_os_and_cpu(monkeypatch, platform_name, machine, suffix, expected):
    monkeypatch.setattr(sys, "platform", platform_name)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    assert pack.release_name(suffix) == expected


@pytest.mark.parametrize("platform_name, tail", [
    ("win32", "FILM_UNIT/FILM_UNIT.exe"), ("darwin", "FILM_UNIT/FILM UNIT.app/Contents/MacOS/FILM_UNIT"), ("linux", "FILM_UNIT/FILM_UNIT")])
def test_the_frozen_check_finds_the_program_inside_each_layout(monkeypatch, platform_name, tail):
    monkeypatch.setattr(sys, "platform", platform_name)
    assert frozen_check.executable("/x").as_posix() == "/x/" + tail


def test_deb_package_depends_on_the_system_ffmpeg_and_is_well_formed():
    text = pack.deb_control("0.3.0", "amd64")
    fields = dict(line.split(": ", 1) for line in text.splitlines() if line and not line.startswith(" "))
    assert fields["Package"] == "film-unit" and fields["Version"] == "0.3.0" and fields["Architecture"] == "amd64"
    assert "ffmpeg" in fields["Depends"].split(", ")
    assert text.endswith("\n") and "\n\n" not in text
    entry = pack.desktop_entry()
    assert "Exec=/opt/film-unit/FILM_UNIT" in entry and "Icon=film-unit" in entry and "Terminal=false" in entry


@pytest.fixture
def stage(tmp_path):
    folder = tmp_path / "stage" / "FILM_UNIT"
    (folder / "_internal").mkdir(parents=True)
    program = folder / "FILM_UNIT"
    program.write_bytes(b"#!/bin/sh\necho hi\n")
    program.chmod(0o755)
    (folder / "_internal/data.txt").write_text("한글 데이터", encoding="utf-8")
    if os.name != "nt":
        (folder / "_internal/link").symlink_to("data.txt")
    return folder


@pytest.mark.skipif(os.name == "nt", reason="tar releases are Linux-only; symlinks need POSIX")
def test_tar_release_keeps_the_program_executable_and_symlinks(stage, tmp_path):
    archive = pack.make_tar(stage, tmp_path / "out.tar.gz")
    unpacked = pack.extract(archive, tmp_path / "unpacked")
    program = unpacked / "FILM_UNIT/FILM_UNIT"
    assert os.access(program, os.X_OK) and (unpacked / "FILM_UNIT/_internal/link").is_symlink()
    assert (unpacked / "FILM_UNIT/_internal/data.txt").read_text(encoding="utf-8") == "한글 데이터"


def test_zip_release_unpacks_to_one_top_level_folder(stage, tmp_path, monkeypatch):
    if sys.platform == "darwin":                 # the macOS zip is made with ditto from a real .app folder
        pytest.skip("covered by the macOS build itself")
    archive = pack.make_zip(stage, tmp_path / "out.zip")
    unpacked = pack.extract(archive, tmp_path / "unpacked")
    assert [p.name for p in unpacked.iterdir()] == ["FILM_UNIT"]
    assert (unpacked / "FILM_UNIT/_internal/data.txt").read_text(encoding="utf-8") == "한글 데이터"


def test_notices_record_inputs_and_hash_every_shipped_file_but_not_themselves(stage, tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "vendor/fonts").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "vendor/fonts/OFL.txt").write_text("OFL", encoding="utf-8")
    (root / "vendor/fonts/source.json").write_text('{"font_sha256": "abc"}', encoding="utf-8")
    (root / "docs/DESKTOP_APPS.md").write_text("# start", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr(pack, "ROOT", root)
    monkeypatch.setenv("SOURCE_SHA", "deadbeef")
    inventory = pack.write_notices(stage)
    info = read(stage / "BUILD_INFO.json")
    assert info["source_sha"] == "deadbeef" and info["version"] == "1.2.3" and info["signed"] is False
    assert "not bundled" in info["ffmpeg"] and "streamlit" in {k.lower() for k in info["dependencies"]}
    assert (stage / "START_HERE.md").read_text(encoding="utf-8") == "# start"
    assert "SHA256SUMS.json" not in inventory and "FILM_UNIT" in inventory and "THIRD_PARTY_LICENSES/NotoSansKR_OFL.txt" in inventory
    assert inventory["_internal/data.txt"] == hashlib.sha256("한글 데이터".encode()).hexdigest()


def test_pyinstaller_spec_only_names_files_that_exist():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "desktop/film_unit.spec").read_text(encoding="utf-8")
    named = re.findall(r'root / "([^"]+)"', spec)
    assert "docs/DESKTOP_APPS.md" in named
    assert '(root / "app").glob("*.py")' in spec and len(list((root / "app").glob("*.py"))) >= 4   # every panel module ships
    for relative in named:
        if not relative.startswith("vendor/"):              # fetched at build time by prepare_desktop_assets.py
            assert (root / relative).exists(), relative
    assert 'sys.platform == "darwin"' in spec and "BUNDLE(" in spec


def test_animation_ui_and_segment_engine_ship_in_the_bundle():
    """ANIM-012: the desktop bundle must carry the new animation modules and the
    frozen smoke must actually import the path-A/B/C panel module."""
    root = Path(__file__).resolve().parents[1]
    spec = (root / "desktop/film_unit.spec").read_text(encoding="utf-8")
    # app/*.py ships as data, engine submodules as hidden imports — so
    # animation_ui.py, segment_gen.py and segment_fake.py ride along without
    # being named individually.
    assert '(root / "app").glob("*.py")' in spec
    assert 'collect_submodules("engine")' in spec
    assert (root / "app/animation_ui.py").is_file()
    assert (root / "engine/segment_gen.py").is_file()
    assert (root / "engine/segment_fake.py").is_file()
    smoke = (root / "desktop/smoke.py").read_text(encoding="utf-8")
    assert '"app.animation_ui"' in smoke   # frozen smoke imports every panel
