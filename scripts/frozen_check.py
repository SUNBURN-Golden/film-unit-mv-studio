"""Unpack a release archive the way a user would, then run the frozen app's own checks.

    python scripts/frozen_check.py dist/release/FILM_UNIT-Linux-x64.tar.gz [evidence-folder]

Runs `--self-test` (real analysis, Korean subtitles, Preview, integrity check and the
Streamlit script) and `--smoke-gui` (the launcher window starts and stops the local
server). On Linux run it under xvfb-run so Tk has a display.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.package_desktop import APP, TOP, extract  # noqa: E402

EVIDENCE_FILES = ("smoke.json", "gui.json", "smoke_project/builds/B0001/MASTER_SUBBED.mp4",
                  "smoke_project/builds/B0001/subtitle_report.json")


def executable(unpacked):
    """The frozen program inside the folder that holds FILM_UNIT/, for the running OS."""
    base = Path(unpacked) / TOP
    if sys.platform == "win32":
        return base / "FILM_UNIT.exe"
    if sys.platform == "darwin":
        return base / APP / "Contents/MacOS/FILM_UNIT"
    return base / "FILM_UNIT"


def run(command, timeout, label):
    print(f"$ {label}", flush=True)
    try:
        result = subprocess.run(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{label}: timed out after {timeout} s")
    if result.returncode != 0:
        raise SystemExit(f"{label}: exit code {result.returncode}")


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    archive = Path(argv[0]).resolve()
    keep = Path(argv[1]).resolve() if len(argv) > 1 else ROOT / "desktop-evidence"
    with tempfile.TemporaryDirectory(prefix="film-unit-unpacked-") as unpacked:
        extract(archive, unpacked)
        program = executable(unpacked)
        if not program.is_file():
            raise SystemExit(f"Archive does not contain {program.relative_to(unpacked)}")
        if not os.access(program, os.X_OK):
            raise SystemExit("The unpacked program lost its executable permission")
        # A folder name with Korean text, a space and a quote catches path-handling bugs.
        evidence = Path(tempfile.mkdtemp(prefix="MV 한글 ' evidence-"))
        run([str(program), "--self-test", str(evidence)], 600, "--self-test")
        if not (evidence / "smoke.json").is_file():
            raise SystemExit("The self test wrote no report")
        report = evidence / "gui.json"
        run([str(program), "--smoke-gui", str(report)], 180, "--smoke-gui")
        if not report.is_file():
            raise SystemExit("The launcher window test wrote no report")
        keep.mkdir(parents=True, exist_ok=True)
        for name in EVIDENCE_FILES:
            if (evidence / name).is_file():
                shutil.copyfile(evidence / name, keep / Path(name).name)
    print("Frozen app checks passed", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
