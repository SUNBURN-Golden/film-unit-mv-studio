"""Turn the PyInstaller output into the files a user downloads, on the OS that built it.

    Windows  dist/release/FILM_UNIT-Windows-x64.zip
    macOS    dist/release/FILM_UNIT-macOS-arm64.zip     (holds "FILM UNIT.app")
    Linux    dist/release/FILM_UNIT-Linux-x64.tar.gz and film-unit_<version>_amd64.deb

Every archive unpacks to one FILM_UNIT/ folder that also carries START_HERE.md,
THIRD_PARTY_LICENSES/, BUILD_INFO.json and SHA256SUMS.json. On macOS those files sit
beside the .app, never inside it, because changing a signed bundle breaks its signature.
"""
from importlib import metadata
from pathlib import Path
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOP = "FILM_UNIT"
APP = "FILM UNIT.app"
# Runtime libraries the bundled Tk needs on a desktop Linux that is not a full Ubuntu.
DEB_DEPENDS = ("ffmpeg", "libc6 (>= 2.35)", "libx11-6", "libxft2", "libfontconfig1", "libfreetype6", "libxrender1", "libxext6")
DEB_RECOMMENDS = ("xdg-utils", "fonts-noto-cjk")


def version():
    return re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)


def architecture():
    return "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"


def os_label():
    return {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")


def release_name(suffix):
    return f"{TOP}-{os_label()}-{architecture()}{suffix}"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ffmpeg_statement():
    from desktop.ffmpeg_setup import SOURCES, VERSION, platform_key
    entry = SOURCES.get(platform_key(), {})
    return (f"Downloaded by the user at first run from {entry.get('provider', 'the publisher')} "
            f"(FFmpeg {VERSION}, SHA-256 pinned in desktop/ffmpeg_setup.py); not bundled. "
            "The Linux .deb uses the distribution's ffmpeg package instead.")


def write_notices(stage):
    """Third-party licences, the exact build inputs and a hash of every shipped file."""
    stage = Path(stage)
    notices = stage / "THIRD_PARTY_LICENSES"
    notices.mkdir(parents=True, exist_ok=True)
    packages = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        packages[name] = dist.version
        for file in dist.files or []:
            if any(marker in file.name.lower() for marker in ("license", "copying", "notice")):
                source = Path(dist.locate_file(file))
                if source.is_file() and source.stat().st_size < 1_000_000:
                    target = notices / name / str(file).replace("/", "_").replace("\\", "_")
                    target.parent.mkdir(exist_ok=True)
                    shutil.copyfile(source, target)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copyfile(python_license, notices / "PYTHON_LICENSE.txt")
    shutil.copyfile(ROOT / "vendor/fonts/OFL.txt", notices / "NotoSansKR_OFL.txt")
    shutil.copyfile(ROOT / "docs/DESKTOP_APPS.md", stage / "START_HERE.md")
    info = {"source_sha": os.environ.get("SOURCE_SHA", "local-unversioned"), "version": version(),
            "python": sys.version, "platform": f"{os_label()}-{architecture()}", "dependencies": packages,
            "ffmpeg": ffmpeg_statement(),
            "font": json.loads((ROOT / "vendor/fonts/source.json").read_text(encoding="utf-8")),
            "signed": False}
    (stage / "BUILD_INFO.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    inventory = {}
    for path in sorted(stage.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS.json":
            inventory[path.relative_to(stage).as_posix()] = digest(path)
    (stage / "SHA256SUMS.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    return inventory


def stage_folder(dist):
    """The FILM_UNIT folder that will be archived. On macOS the .app is copied into a clean one."""
    dist = Path(dist)
    if sys.platform != "darwin":
        return dist / TOP
    staged = dist / "mac-stage" / TOP
    shutil.rmtree(staged.parent, ignore_errors=True)
    staged.mkdir(parents=True)
    # ditto keeps symlinks, permissions and the ad-hoc signature intact.
    subprocess.run(["ditto", str(dist / APP), str(staged / APP)], check=True)
    return staged


def make_zip(stage, target):
    stage, target = Path(stage), Path(target)
    target.unlink(missing_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(stage), str(target)], check=True)
        return target
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(stage.rglob("*")):
            archive.write(path, (Path(TOP) / path.relative_to(stage)).as_posix())
    return target


def make_tar(stage, target):
    stage, target = Path(stage), Path(target)
    target.unlink(missing_ok=True)
    with tarfile.open(target, "w:gz") as archive:
        archive.add(stage, arcname=TOP)         # keeps permission bits and symlinks
    return target


def extract(archive, destination):
    """Unpack a release file the way a user does, returning the folder that holds FILM_UNIT/."""
    archive, destination = Path(archive), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".tar.gz"):
        with tarfile.open(archive) as tar:
            tar.extractall(destination, filter="tar")
    elif sys.platform == "darwin":
        subprocess.run(["ditto", "-x", "-k", str(archive), str(destination)], check=True)
    else:
        with zipfile.ZipFile(archive) as z:
            z.extractall(destination)
    return destination


def deb_control(app_version, arch):
    return "\n".join([
        "Package: film-unit",
        f"Version: {app_version}",
        "Section: video",
        "Priority: optional",
        f"Architecture: {arch}",
        f"Depends: {', '.join(DEB_DEPENDS)}",
        f"Recommends: {', '.join(DEB_RECOMMENDS)}",
        "Maintainer: BeautifulMind-JT <noreply@users.noreply.github.com>",
        "Homepage: https://github.com/BeautifulMind-JT/film-unit-mv-studio",
        "Description: FILM UNIT music video compiler",
        " Local, file-based music video compiler with a browser control panel.",
        " Model choices and API keys stay in the user's own folder.",
        ""])


def desktop_entry():
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=FILM UNIT",
        "Comment=Music video compiler", "Exec=/opt/film-unit/FILM_UNIT", "Icon=film-unit",
        "Terminal=false", "Categories=AudioVideo;Video;", ""])


def make_deb(stage, dist):
    if not shutil.which("dpkg-deb"):
        print("dpkg-deb not found; skipping the .deb", flush=True)
        return None
    arch = "arm64" if architecture() == "arm64" else "amd64"
    package = Path(dist) / "deb-root"
    shutil.rmtree(package, ignore_errors=True)
    shutil.copytree(stage, package / "opt/film-unit", symlinks=True)
    (package / "usr/bin").mkdir(parents=True)
    (package / "usr/bin/film-unit").symlink_to("/opt/film-unit/FILM_UNIT")
    apps = package / "usr/share/applications"
    apps.mkdir(parents=True)
    (apps / "film-unit.desktop").write_text(desktop_entry(), encoding="utf-8")
    icons = package / "usr/share/icons/hicolor/256x256/apps"
    icons.mkdir(parents=True)
    shutil.copyfile(ROOT / "vendor/icon/icon-256.png", icons / "film-unit.png")
    (package / "DEBIAN").mkdir()
    (package / "DEBIAN/control").write_text(deb_control(version(), arch), encoding="utf-8")
    target = Path(dist) / "release" / f"film-unit_{version()}_{arch}.deb"
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(package), str(target)], check=True)
    shutil.rmtree(package)
    return target


def main():
    dist = ROOT / "dist"
    release = dist / "release"
    release.mkdir(parents=True, exist_ok=True)
    stage = stage_folder(dist)
    write_notices(stage)
    files = []
    if sys.platform.startswith("linux"):
        files += [make_tar(stage, release / release_name(".tar.gz")), make_deb(stage, dist)]
    else:
        files.append(make_zip(stage, release / release_name(".zip")))
    files = [f for f in files if f]
    (release / "SHA256SUMS.txt").write_text("".join(f"{digest(f)}  {f.name}\n" for f in files), encoding="utf-8")
    for f in files:
        print(f"{f.name}  {f.stat().st_size // (1024 * 1024)} MB  {digest(f)}")


if __name__ == "__main__":
    main()
