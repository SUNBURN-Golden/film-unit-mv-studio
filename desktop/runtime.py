"""Separate immutable application resources from writable user projects."""
from pathlib import Path
import os
import shutil
import sys


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def data_root():
    override = os.environ.get("FILM_UNIT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "FILM_UNIT"


def configure_runtime():
    root = data_root()
    for name in ("projects", "logs", "cache/numba", "cache/matplotlib"):
        (root / name).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("FILM_UNIT_PROJECTS", str(root / "projects"))
    os.environ["NUMBA_CACHE_DIR"] = str(root / "cache/numba")
    os.environ["MPLCONFIGDIR"] = str(root / "cache/matplotlib")
    os.environ["MPLBACKEND"] = "Agg"
    os.environ["PYTHONUTF8"] = "1"
    from desktop.ffmpeg_setup import install_dir
    binaries = install_dir(root) / "bin"
    if binaries.is_dir():
        os.environ["PATH"] = str(binaries) + os.pathsep + os.environ.get("PATH", "")
    font = resource_root() / "vendor/fonts/NotoSansKR.ttf"
    if font.is_file():
        os.environ["FILM_UNIT_FONT_FILE"] = str(font)
    return root


def configure_project_font(project):
    """Copy a bundled font into new projects; never change an existing choice."""
    from engine.core import read, write
    source = os.environ.get("FILM_UNIT_FONT_FILE")
    if not source:
        return
    p = Path(project)
    config = read(p / "project.yaml")
    if config.get("subtitles", {}).get("font_file"):
        return
    target = p / "input/fonts/NotoSansKR.ttf"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    config["subtitles"] = {**config.get("subtitles", {}),
                           "font_file": "input/fonts/NotoSansKR.ttf", "font_name": "Noto Sans KR"}
    write(p / "project.yaml", config)
