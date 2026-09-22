"""Optional first-run download, pinned and checked before any binary executes.

FFmpeg is downloaded directly by its user, not redistributed inside FILM UNIT.
The compiler also accepts an already installed FFmpeg/ffprobe on PATH.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
import zipfile

VERSION = "9.0.2"
URL = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"


def install_dir(root):
    return Path(root) / "runtime" / f"ffmpeg-{VERSION}"


def is_ready():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def extract_checked(archive, destination, expected_sha=SHA256):
    archive, destination = Path(archive), Path(destination)
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected_sha:
        raise RuntimeError("FFmpeg download checksum mismatch; nothing was installed")
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as z:
        # Copy only fixed filenames; never extract arbitrary archive paths.
        for filename in ("ffmpeg.exe", "ffprobe.exe"):
            matches = [entry for entry in z.infolist() if entry.filename.endswith("/bin/" + filename)]
            if len(matches) != 1 or matches[0].file_size > 250_000_000:
                raise RuntimeError(f"Unexpected FFmpeg archive: {filename}")
            target = destination / "bin" / filename
            target.parent.mkdir(exist_ok=True)
            with z.open(matches[0]) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
        for entry in z.infolist():
            basename = entry.filename.rsplit("/", 1)[-1]
            if basename.lower() in {"license", "license.txt", "readme.txt"} and entry.file_size < 1_000_000:
                (destination / basename).write_bytes(z.read(entry))
    (destination / "download.json").write_text(json.dumps({"url": URL, "sha256": actual, "version": VERSION}), encoding="utf-8")


def ensure_ffmpeg(root, progress=lambda message: None):
    final = install_dir(root)
    if (final / "bin/ffmpeg.exe").is_file() and (final / "bin/ffprobe.exe").is_file():
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    progress("FFmpeg 내려받는 중… 최초 실행에만 필요합니다.")
    with tempfile.TemporaryDirectory(prefix="ffmpeg-setup-", dir=final.parent) as work:
        archive = Path(work) / "download.zip"
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
            received = 0
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > 300_000_000:
                    raise RuntimeError("FFmpeg download exceeds expected size")
                output.write(block)
                progress(f"FFmpeg 내려받는 중… {received // (1024 * 1024)} MB")
        progress("다운로드 무결성 확인 중…")
        staged = Path(work) / "installed"
        extract_checked(archive, staged)
        try:
            staged.rename(final)
        except FileExistsError:
            # Another launcher can finish the same verified setup concurrently.
            if not all((final / "bin" / name).is_file() for name in ("ffmpeg.exe", "ffprobe.exe")):
                raise
    return final
