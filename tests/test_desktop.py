"""Cross-platform regressions for packaging boundaries, not renderer behavior."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from desktop.ffmpeg_setup import extract_checked
from desktop.runtime import configure_runtime, data_root, resource_root
from engine.core import FilmError, atomic_text, project_mutex, read, write


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
