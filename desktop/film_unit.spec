# Build on the OS you are packaging for (PyInstaller does not cross-compile):
#   python -m PyInstaller --noconfirm desktop/film_unit.spec
# Windows -> dist/FILM_UNIT/FILM_UNIT.exe, Linux -> dist/FILM_UNIT/FILM_UNIT,
# macOS -> dist/FILM UNIT.app (plus dist/FILM_UNIT).
import re
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
version = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
icons = root / "vendor/icon"
icon = {"win32": icons / "icon.ico", "darwin": icons / "icon.icns"}.get(sys.platform)
icon = str(icon) if icon and icon.is_file() else None

datas = [(str(root / "app/control_panel.py"), "app"),
         (str(root / "app/models_ui.py"), "app"),
         (str(root / "app/__init__.py"), "app"),
         (str(root / "presets"), "presets"),
         (str(root / "templates"), "templates"),
         (str(root / "vendor/fonts"), "vendor/fonts"),
         (str(root / ".streamlit/config.toml"), ".streamlit"),
         (str(root / "docs/DESKTOP_APPS.md"), "docs")]
datas += collect_data_files("streamlit") + copy_metadata("streamlit")
datas += collect_data_files("librosa", include_py_files=True, excludes=["**/__pycache__"])
# Streamlit runs app Python dynamically, so dependency discovery must include
# engine modules that are not imported directly by the launcher.
hidden = collect_submodules("engine") + collect_submodules("streamlit")
a = Analysis([str(root / "desktop/entry.py")], pathex=[str(root)],
             binaries=[], datas=datas, hiddenimports=hidden,
             hookspath=[], runtime_hooks=[], excludes=["IPython", "pytest"],
             hooksconfig={"matplotlib": {"backends": ["Agg"]}}, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="FILM_UNIT",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FILM_UNIT")

if sys.platform == "darwin":
    # Unsigned/ad-hoc signed: macOS asks the user to confirm the first launch (see docs/DESKTOP_APPS.md).
    app = BUNDLE(coll, name="FILM UNIT.app", icon=icon, bundle_identifier="com.beautifulmind.filmunit",
                 info_plist={"CFBundleName": "FILM UNIT", "CFBundleDisplayName": "FILM UNIT",
                             "CFBundleShortVersionString": version, "CFBundleVersion": version,
                             "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"})
