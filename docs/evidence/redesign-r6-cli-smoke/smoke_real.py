import sys, os, tempfile, pathlib, json, shutil
sys.path.insert(0, "tests")
tmp = pathlib.Path(tempfile.mkdtemp(prefix="r6smoke-"))
os.environ["FILM_UNIT_HOME"] = str(tmp / "home")
from engine.audio import analyze, synth_test_audio
from engine.core import init_project, set_production
from engine.production import make_package
from engine.compiler import import_asset
from engine import cli, conti_brief
import test_conti_brief as t
audio = synth_test_audio(tmp/"t.wav", seconds=30)
p = init_project(tmp, "smoke", audio, "A woman remembers her old kitchen.", "첫 줄 가사\n둘째 줄 가사", synthetic=True, aspect="16:9")
analyze(p); make_package(p)
t._conti(p, "S001")
import_asset(p, "S001", t._clip(tmp/"take.mp4", 6), kind="draft")
conti_brief.check_take(p, "S001")
set_production(p, rewrite_driver="claude_cli")
print("PROJECT", p)
rc = cli.main(["conti-loop", str(p), "S001"])
print("RC", rc)
