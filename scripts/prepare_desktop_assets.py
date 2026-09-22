"""Build-time font fetch and dependency notices (no video provider requests)."""
from pathlib import Path
import hashlib
import json
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FONT_REVISION = "b38c5c93af322c45f633e17ac440ec1e6c94d489"
FONT_SHA256 = "194018e6b2b293a7964f037b25c0249ce1418bc9ab3c971060a03aa57861e252"
BASE = f"https://raw.githubusercontent.com/google/fonts/{FONT_REVISION}/ofl/notosanskr/"


def main():
    fonts = ROOT / "vendor/fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    for remote, local in [("NotoSansKR%5Bwght%5D.ttf", "NotoSansKR.ttf"), ("OFL.txt", "OFL.txt")]:
        with urllib.request.urlopen(BASE + remote, timeout=60) as response:
            (fonts / local).write_bytes(response.read())
    manifest = {"font_revision": FONT_REVISION, "font_sha256": hashlib.sha256((fonts / "NotoSansKR.ttf").read_bytes()).hexdigest()}
    if manifest["font_sha256"] != FONT_SHA256:
        raise RuntimeError("Pinned Noto Sans KR font checksum mismatch")
    (fonts / "source.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
