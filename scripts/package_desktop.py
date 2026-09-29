"""Record exact build inputs and licenses next to the portable executable."""
from pathlib import Path
from importlib import metadata
import hashlib
import json
import os
import shutil
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    bundle = root / "dist/FILM_UNIT"
    notices = bundle / "THIRD_PARTY_LICENSES"
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
    shutil.copyfile(root / "vendor/fonts/OFL.txt", notices / "NotoSansKR_OFL.txt")
    shutil.copyfile(root / "docs/WINDOWS_APP.md", bundle / "START_HERE.md")
    info = {"source_sha": os.environ.get("SOURCE_SHA", "local-unversioned"),
            "python": sys.version, "platform": sys.platform, "dependencies": packages,
            "ffmpeg": "User downloads pinned Gyan 9.0.2 at first run; not bundled",
            "font": json.loads((root / "vendor/fonts/source.json").read_text(encoding="utf-8"))}
    (bundle / "BUILD_INFO.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    inventory = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.json":
            with path.open("rb") as stream:
                inventory[path.relative_to(bundle).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    (bundle / "SHA256SUMS.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
