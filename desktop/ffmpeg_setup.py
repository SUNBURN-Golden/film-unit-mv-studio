"""Optional first-run download, pinned and checked before any binary executes.

FFmpeg is downloaded directly by its user, not redistributed inside FILM UNIT.
The compiler also accepts an already installed FFmpeg/ffprobe on PATH, and the
Linux .deb depends on the distribution's own ffmpeg package instead.

Every archive below was downloaded and its SHA-256 compared with the value the
publisher lists (2026-09-29). The Linux amd64 build was run to confirm it has
libx264, aac and the libass `ass`/`subtitles` filters the compiler needs.
"""
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile

VERSION = "9.0.2"
GYAN = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
GYAN_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
RIEDL = "https://ffmpeg.martin-riedl.de/download/"
MAX_DOWNLOAD = 300_000_000
MAX_BINARY = 250_000_000

# One single-binary zip per tool for macOS and Linux (Martin Riedl release builds).
SOURCES = {
    "windows-amd64": {"provider": "Gyan.dev", "archive": {"url": GYAN, "sha256": GYAN_SHA256}},
    "macos-amd64": {"provider": "martin-riedl.de", "files": {
        "ffmpeg": (RIEDL + "macos/amd64/1789931006_9.0.2/ffmpeg.zip", "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4"),
        "ffprobe": (RIEDL + "macos/amd64/1789931006_9.0.2/ffprobe.zip", "2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd")}},
    "macos-arm64": {"provider": "martin-riedl.de", "files": {
        "ffmpeg": (RIEDL + "macos/arm64/1789931890_9.0.2/ffmpeg.zip", "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924"),
        "ffprobe": (RIEDL + "macos/arm64/1789931890_9.0.2/ffprobe.zip", "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6")}},
    "linux-amd64": {"provider": "martin-riedl.de", "files": {
        "ffmpeg": (RIEDL + "linux/amd64/1789931100_9.0.2/ffmpeg.zip", "fa8ecf4abbd290d98f7d188b8649cc6b391ae209a98452be955a15aab1909d7f"),
        "ffprobe": (RIEDL + "linux/amd64/1789931100_9.0.2/ffprobe.zip", "3f428c49070be3d24ec338602b76d412e401ffcb8a5641ef0e729181a232fc32")}},
    "linux-arm64": {"provider": "martin-riedl.de", "files": {
        "ffmpeg": (RIEDL + "linux/arm64/1789931697_9.0.2/ffmpeg.zip", "93a76ae90db5474eecdf951a729857c64f3de23567228d6a7d5e6e8e3cd1021b"),
        "ffprobe": (RIEDL + "linux/arm64/1789931697_9.0.2/ffprobe.zip", "bcbe80fb741c180083327afaf5434812e006b33cacde2016b9aeaf6936128330")}},
}
# Kept for callers and tests written against the Windows-only version.
URL, SHA256 = GYAN, GYAN_SHA256


def platform_key():
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    if sys.platform == "win32":
        return "windows-amd64"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    if sys.platform.startswith("linux"):
        return f"linux-{arch}"
    return sys.platform + "-" + arch


def source():
    key = platform_key()
    if key not in SOURCES:
        raise RuntimeError(f"FFmpeg 자동 설치를 지원하지 않는 컴퓨터입니다({key}). FFmpeg를 직접 설치하세요.")
    return SOURCES[key]


def provider_name():
    try:
        return source()["provider"]
    except RuntimeError:
        return "공식 배포처"


def binary_names():
    return ("ffmpeg.exe", "ffprobe.exe") if sys.platform == "win32" else ("ffmpeg", "ffprobe")


def install_dir(root):
    return Path(root) / "runtime" / f"ffmpeg-{VERSION}"


def is_ready():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def extract_checked(archive, destination, expected_sha=GYAN_SHA256):
    """Windows layout: one archive holding <folder>/bin/ffmpeg.exe and ffprobe.exe."""
    archive, destination = Path(archive), Path(destination)
    actual = _digest(archive)
    if actual != expected_sha:
        raise RuntimeError("FFmpeg download checksum mismatch; nothing was installed")
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        # Copy only fixed filenames; never extract arbitrary archive paths.
        for filename in ("ffmpeg.exe", "ffprobe.exe"):
            matches = [entry for entry in z.infolist() if entry.filename.endswith("/bin/" + filename)]
            if len(matches) != 1 or matches[0].file_size > MAX_BINARY:
                raise RuntimeError(f"Unexpected FFmpeg archive: {filename}")
            target = destination / "bin" / filename
            target.parent.mkdir(exist_ok=True)
            with z.open(matches[0]) as source_file, target.open("wb") as output:
                shutil.copyfileobj(source_file, output)
        for entry in z.infolist():
            basename = entry.filename.rsplit("/", 1)[-1]
            if basename.lower() in {"license", "license.txt", "readme.txt"} and entry.file_size < 1_000_000:
                (destination / basename).write_bytes(z.read(entry))
    (destination / "download.json").write_text(json.dumps({"url": GYAN, "sha256": actual, "version": VERSION}), encoding="utf-8")


def extract_single(archive, destination, name, expected_sha):
    """macOS/Linux layout: a zip holding exactly one file, the tool itself."""
    archive, destination = Path(archive), Path(destination)
    actual = _digest(archive)
    if actual != expected_sha:
        raise RuntimeError("FFmpeg download checksum mismatch; nothing was installed")
    with zipfile.ZipFile(archive) as z:
        entries = [e for e in z.infolist() if not e.is_dir()]
        if [e.filename for e in entries] != [name] or entries[0].file_size > MAX_BINARY:
            raise RuntimeError(f"Unexpected FFmpeg archive: {name}")
        target = destination / "bin" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(entries[0]) as source_file, target.open("wb") as output:
            shutil.copyfileobj(source_file, output)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return actual


# Some download hosts refuse Python's default "Python-urllib" client name (HTTP 403).
USER_AGENT = "FILM-UNIT-Setup/0.3 (+https://github.com/BeautifulMind-JT/film-unit-mv-studio)"


def _download(url, path, progress):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, Path(path).open("wb") as output:
        expected = response.headers.get("Content-Length")
        received = 0
        while block := response.read(1024 * 1024):
            received += len(block)
            if received > MAX_DOWNLOAD:
                raise RuntimeError("FFmpeg download exceeds expected size")
            output.write(block)
            progress(f"FFmpeg 내려받는 중… {received // (1024 * 1024)} MB")
    # A dropped connection ends a read quietly; say so instead of reporting a checksum failure.
    if expected and expected.isdigit() and received != int(expected):
        raise RuntimeError(f"FFmpeg download was cut off ({received} of {expected} bytes); nothing was installed. Please try again.")


def installed(final):
    return all((Path(final) / "bin" / name).is_file() for name in binary_names())


def ensure_ffmpeg(root, progress=lambda message: None):
    final = install_dir(root)
    if installed(final):
        return final
    chosen = source()
    final.parent.mkdir(parents=True, exist_ok=True)
    progress("FFmpeg 내려받는 중… 최초 실행에만 필요합니다.")
    with tempfile.TemporaryDirectory(prefix="ffmpeg-setup-", dir=final.parent) as work:
        staged = Path(work) / "installed"
        if "archive" in chosen:
            archive = Path(work) / "download.zip"
            _download(chosen["archive"]["url"], archive, progress)
            progress("다운로드 무결성 확인 중…")
            extract_checked(archive, staged, chosen["archive"]["sha256"])
        else:
            staged.mkdir()
            record = {"version": VERSION, "provider": chosen["provider"], "files": {}}
            for name, (url, checksum) in chosen["files"].items():
                archive = Path(work) / f"{name}.zip"
                _download(url, archive, progress)
                progress("다운로드 무결성 확인 중…")
                record["files"][name] = {"url": url, "sha256": extract_single(archive, staged, name, checksum)}
            (staged / "download.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        try:
            staged.rename(final)
        except OSError:
            # Another launcher can finish the same verified setup concurrently.
            if not installed(final):
                raise
    return final
