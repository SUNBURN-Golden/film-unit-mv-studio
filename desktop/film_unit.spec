# Build on Windows x64: python -m PyInstaller --noconfirm desktop/film_unit.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = [(str(root / "app/control_panel.py"), "app"),
         (str(root / "presets"), "presets"),
         (str(root / "templates"), "templates"),
         (str(root / "vendor/fonts"), "vendor/fonts"),
         (str(root / ".streamlit/config.toml"), ".streamlit"),
         (str(root / "docs/WINDOWS_APP.md"), "docs")]
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
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FILM_UNIT")
