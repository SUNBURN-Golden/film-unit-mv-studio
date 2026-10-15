"""REDESIGN_W00 R6: the subscription-CLI driver, tested against a FAKE `claude` executable.

These are fake-CLI tests. A small script named `claude` is put first on PATH; it
answers `auth status` and `-p` the way the real Claude Code CLI 2.1.290 was seen
to (a JSON envelope with `type`, `is_error`, `result` and `modelUsage`; checked
once by hand on 2026-10-15), and records what it was called with. They prove
FILM UNIT's side of the contract: which flags and environment it passes, that it
refuses anything but a subscription login, enforces a monotonic timeout, keeps the
raw answer and model as evidence, validates before storing, and falls back to the
paste-it-yourself path. They do NOT show that a real model judges or rewrites a
real take well; that needs R5.
"""
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from engine import cli, conti, conti_brief, conti_cli_driver as driver
from engine.core import FilmError, read, set_production
from engine.settings import set_settings
from test_conti_brief import _clip, _conti, _shot, project  # noqa: F401  (project is a fixture)
from test_conti_revision_loop import _answer, _take

FAKE = '''#!{python}
import json, os, subprocess, sys, time
state = os.environ["FAKE_CLAUDE_DIR"]
cfg = json.load(open(os.path.join(state, "config.json")))
args = sys.argv[1:]
log = open(os.path.join(state, "calls.jsonl"), "a")
if args[:2] == ["auth", "status"]:
    log.write(json.dumps({{"call": "auth"}}) + "\\n")
    print(json.dumps(cfg["auth"]))
    sys.exit(0)
prompt = sys.stdin.read()
n = sum(1 for line in open(os.path.join(state, "calls.jsonl")) if '"call": "ask"' in line)
log.write(json.dumps({{"call": "ask", "argv": args, "prompt": prompt, "cwd": os.getcwd(),
                      "leaked": sorted(k for k in os.environ if k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")))}}) + "\\n")
log.close()
mode = cfg["mode"]
if mode == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.path.join(state, "pids"), "w").write(f"{{os.getpid()}} {{child.pid}}")
    time.sleep(120)
if mode == "exit1":
    sys.stderr.write("boom: usage limit reached"); sys.exit(1)
if mode == "garbage":
    print("not json at all"); sys.exit(0)
answers = cfg["answers"]
text = answers[n] if n < len(answers) else ""
print(json.dumps({{"type": "result", "subtype": "success", "is_error": mode == "error", "result": text,
                  "modelUsage": {{"fake-sonnet": {{"outputTokens": 1}}, "fake-fable": {{"outputTokens": 900}}}}}}))
'''
LOGGED_IN = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}


class Fake:
    def __init__(self, root, mp):
        self.dir = root / "fake"
        self.dir.mkdir()
        exe = root / "bin" / "claude"
        exe.parent.mkdir()
        exe.write_text(FAKE.format(python=sys.executable))
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        mp.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ['PATH']}")
        mp.setenv("FAKE_CLAUDE_DIR", str(self.dir))
        mp.delenv("FILM_UNIT_CLAUDE_CMD", raising=False)
        mp.delenv("FILM_UNIT_CLAUDE_TIMEOUT", raising=False)
        self.set()

    def set(self, answers=(), mode="ok", auth=LOGGED_IN):
        (self.dir / "config.json").write_text(json.dumps({"answers": list(answers), "mode": mode, "auth": auth}))
        (self.dir / "calls.jsonl").write_text("")

    def calls(self, kind="ask"):
        return [c for c in map(json.loads, (self.dir / "calls.jsonl").read_text().splitlines()) if c["call"] == kind]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return Fake(tmp_path, monkeypatch)


def _failed_take(project, tmp_path, reason="the camera never moves"):
    _conti(project, "S001")
    take = _take(project, tmp_path, "t1")
    conti_brief.record_verdicts(project, "S001", take["take_sha256"],
                                {1: "PASS", 2: "PASS", 3: {"verdict": "FAIL", "reason": reason}}, "judge")
    return take


def _loop(project, *extra):
    return cli.main(["conti-loop", str(project), "S001", *extra])


def _out(capsys):
    return json.loads(capsys.readouterr().out)[0]


def _evidence(project, kind=None):
    folder = project / "qc/conti/S001/driver"
    rows = sorted(folder.glob("*.json")) if folder.is_dir() else []
    return [(f, json.loads(f.read_text(encoding="utf-8"))) for f in rows if kind in (None, json.loads(f.read_text())["kind"])]


# ---- the setting ---------------------------------------------------------------------------

def test_driver_is_manual_by_default_and_never_calls_the_cli(project, tmp_path, fake, capsys):
    _failed_take(project, tmp_path)
    assert driver.rewrite_driver(project) == "manual"
    assert _loop(project) == 0
    row = _out(capsys)
    assert row["stage"] == "NEEDS_REWRITE" and "driver" not in row, "same output as before the driver existed"
    assert fake.calls("auth") == [] and fake.calls() == []


def test_rewrite_driver_setting_is_validated(project):
    for bad in ("claude", "", None, True):
        with pytest.raises(FilmError, match="rewrite_driver"):
            set_production(project, rewrite_driver=bad)
    with pytest.raises(FilmError, match="sitting_limit"):
        set_production(project, sitting_limit=0)
    with pytest.raises(FilmError, match="unknown production setting"):     # a project file cannot choose the command
        set_production(project, claude_command="rm -rf /")
    assert set_production(project, rewrite_driver="claude_cli", sitting_limit=3)["rewrite_driver"] == "claude_cli"
    assert cli.main(["production", str(project), "--rewrite-driver", "manual", "--sitting-limit", "5"]) == 0
    assert read(project / "project.yaml")["production"]["rewrite_driver"] == "manual"


# ---- rewrite through the CLI --------------------------------------------------------------------

def test_cli_rewrite_is_stored_with_evidence_and_no_api_key(project, tmp_path, fake, capsys, monkeypatch):
    first = _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    moves = [{"at_ms": _shot(project, "S001")["in_ms"] + 1000, "action": "dolly in hard on the face"}]
    fake.set([_answer(project, camera={"type": "dolly", "moves": moves})])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    wall = iter(range(10**9, 10**12, 10**8))
    monkeypatch.setattr(time, "time", lambda: next(wall))        # a jumping wall clock must not matter

    assert _loop(project) == 0
    row = _out(capsys)
    step = row["driver"]["steps"][0]
    assert step["step"] == "rewrite" and step["status"] == "APPLIED" and step["model"] == "fake-fable"
    assert "camera.moves[0].action" in step["conti_changed"]
    assert row["stage"] == "AWAITING_TAKE" and row["next_attempt"] == 2

    [call] = fake.calls()
    assert call["leaked"] == [], "no ANTHROPIC_* / CLAUDE_CODE_USE_* reaches the CLI"
    argv = call["argv"]
    assert argv[:1] == ["-p"] and argv[argv.index("--tools") + 1] == "" and "--no-session-persistence" in argv
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    assert "complete conti document" in call["prompt"] and "the camera never moves" in call["prompt"]
    assert Path(call["cwd"]).name.startswith("filmunit-cli-"), "runs in a scratch folder, not the project"
    assert len(fake.calls("auth")) == 1

    stored = conti.load_conti(project, "S001")
    assert stored["source"] == "llm:claude_cli" and stored["camera"]["moves"] == moves
    [(path, ev)] = _evidence(project, "rewrite")
    assert ev["status"] == "APPLIED" and ev["model"] == "fake-fable" and ev["models"] == ["fake-fable", "fake-sonnet"]
    assert ev["ignored_env"] == ["ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK"]
    assert ev["auth"] == {"authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}
    assert "email" not in json.dumps(ev) and "sk-ant" not in path.read_text() + (path.parent / ev["raw_output_file"]).read_text()
    assert json.loads((path.parent / ev["answer_file"]).read_text())["conti"][0]["shot_id"] == "S001"
    assert json.loads((path.parent / ev["raw_output_file"]).read_text())["type"] == "result"
    assert conti.request_sha(project, ev["request_id"]) == stored["request_sha256"], "linked to the persisted request"

    second = _take(project, tmp_path, "t2", size="1920x1080")
    assert second["attempt"] == 2 and second["revision"]["revises_take"] == first["take_sha256"]


def test_judge_then_rewrite_in_one_pass(project, tmp_path, fake, capsys):
    take = _failed_take(project, tmp_path)
    conti_brief.record_verdicts(project, "S001", take["take_sha256"], {1: "PENDING", 2: "PENDING", 3: "PENDING"}, "reset")
    assert conti_brief.revision_loop(project, "S001")["stage"] == "NEEDS_VERDICTS"
    set_production(project, rewrite_driver="claude_cli")
    judged = json.dumps({"take": take["take_sha256"], "criteria": [
        {"index": 1, "verdict": "PASS", "reason": "red coat in frame 1"},
        {"index": 2, "verdict": "PASS", "reason": "cup resting"},
        {"index": 3, "verdict": "FAIL", "reason": "the four frames are identical in framing"}]})
    moves = [{"at_ms": _shot(project, "S001")["in_ms"] + 1000, "action": "dolly in hard on the face"}]
    fake.set([judged, _answer(project, camera={"type": "dolly", "moves": moves})])

    assert _loop(project) == 0
    row = _out(capsys)
    assert [s["step"] for s in row["driver"]["steps"]] == ["judge", "rewrite"]
    assert [s["status"] for s in row["driver"]["steps"]] == ["APPLIED", "APPLIED"]
    assert row["driver"]["steps"][0]["criteria"] == {"1": "PASS", "2": "PASS", "3": "FAIL"}
    assert row["stage"] == "AWAITING_TAKE"

    judge_call, rewrite_call = fake.calls()
    argv = judge_call["argv"]
    assert argv[argv.index("--tools") + 1] == "Read" and argv[argv.index("--allowedTools") + 1] == "Read"
    frames_dir = str((project / "qc/conti/S001" / take["take_sha256"][:16]).resolve())
    assert str(Path(argv[argv.index("--add-dir") + 1]).resolve()) == frames_dir
    assert "Read tool" in judge_call["prompt"] and "frame_01.jpg" in judge_call["prompt"]
    record = conti_brief.load_check(project, "S001", take["take_sha256"])
    assert record["reviewer"] == "llm:claude_cli" and record["criteria"][2]["reason"].startswith("the four frames")
    assert rewrite_call["argv"][rewrite_call["argv"].index("--tools") + 1] == ""
    assert {ev["kind"] for _, ev in _evidence(project)} == {"judge", "rewrite"}
    assert len(fake.calls("auth")) == 1, "one preflight per conti-loop run"


def test_unsure_stays_pending_for_a_person(project, tmp_path, fake, capsys):
    _conti(project, "S001")
    take = _take(project, tmp_path, "t1")
    set_production(project, rewrite_driver="claude_cli")
    fake.set([json.dumps({"take": take["take_sha256"], "criteria": [
        {"index": 1, "verdict": "PASS", "reason": "coat"}, {"index": 2, "verdict": "UNSURE", "reason": "between samples"},
        {"index": 3, "verdict": "PASS", "reason": "push in"}]})])
    assert _loop(project) == 0
    row = _out(capsys)
    assert row["stage"] == "NEEDS_VERDICTS" and row["driver"]["steps"][0]["unsure"] == [2]
    assert len(fake.calls()) == 1, "the pass stops where a person is needed; nothing is asked twice"


# ---- refusal: subscription login or nothing ------------------------------------------------------

@pytest.mark.parametrize("auth", [
    {"loggedIn": False, "authMethod": "none"},
    {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"},
    {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
    {"nonsense": 1},
])
def test_only_a_subscription_login_may_answer(project, tmp_path, fake, capsys, monkeypatch, auth):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    fake.set([_answer(project, framing="x")], auth=auth)
    assert _loop(project) == 0
    step = _out(capsys)["driver"]["steps"][0]
    assert step["status"] == "FALLBACK_MANUAL" and step["reason"].startswith("REFUSED")
    assert fake.calls() == [], "the model was never asked"
    assert step["manual"]["request_file"].startswith("conti/requests/") and "conti-submit --file" in step["manual"]["next"]
    [(_, ev)] = _evidence(project)
    assert ev["status"] == "REFUSED" and ev["ignored_env"] == ["ANTHROPIC_API_KEY"]
    if auth.get("authMethod") == "none":
        assert "never uses API keys" in step["reason"]


def test_missing_cli_falls_back_to_the_paste_path(project, tmp_path, fake, capsys, monkeypatch):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    monkeypatch.setenv("FILM_UNIT_CLAUDE_CMD", "claude-does-not-exist")
    assert _loop(project) == 0
    row = _out(capsys)
    step = row["driver"]["steps"][0]
    assert step["status"] == "FALLBACK_MANUAL" and "MISSING" in step["reason"]
    assert row["stage"] == "AWAITING_CONTI_CHANGE", "the request exists; the person pastes it"
    request = project / step["manual"]["request_file"]
    assert request.is_file() and "complete conti document" in request.read_text(encoding="utf-8")
    # …and the existing manual path completes the loop with the same request.
    moves = [{"at_ms": _shot(project, "S001")["in_ms"] + 1000, "action": "dolly in"}]
    conti.submit_answer(project, _answer(project, camera={"type": "dolly", "moves": moves}), request_id=step["manual"]["request_id"])
    assert conti_brief.revision_loop(project, "S001")["stage"] == "AWAITING_TAKE"


def test_command_comes_from_user_settings_or_env_not_the_project(project, tmp_path, fake, monkeypatch):
    assert driver.cli_config()["argv"] == ["claude"] and driver.cli_config()["timeout"] == 600
    set_settings("claude_cli", {"command": "my-claude --flag", "timeout_seconds": "42", "model": "claude-fable-5-1"})
    config = driver.cli_config()
    assert config["argv"] == ["my-claude", "--flag"] and config["timeout"] == 42 and config["model"] == "claude-fable-5-1"
    monkeypatch.setenv("FILM_UNIT_CLAUDE_CMD", "other")
    monkeypatch.setenv("FILM_UNIT_CLAUDE_TIMEOUT", "7")
    assert driver.cli_config()["argv"] == ["other"] and driver.cli_config()["timeout"] == 7
    monkeypatch.setenv("FILM_UNIT_CLAUDE_TIMEOUT", "0")
    with pytest.raises(FilmError, match="between 1 and"):
        driver.cli_config()


def test_model_setting_is_passed_through(project, tmp_path, fake, capsys):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    set_settings("claude_cli", {"model": "claude-fable-5-1"})
    fake.set(["{}"])
    assert _loop(project) == 0
    argv = fake.calls()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"


# ---- timeout ------------------------------------------------------------------------------

def test_timeout_is_enforced_and_the_process_group_killed(project, tmp_path, fake, capsys, monkeypatch):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    monkeypatch.setenv("FILM_UNIT_CLAUDE_TIMEOUT", "1")
    fake.set(mode="sleep")
    started = time.monotonic()
    assert _loop(project) == 0
    assert time.monotonic() - started < 20
    step = _out(capsys)["driver"]["steps"][0]
    assert step["status"] == "FALLBACK_MANUAL" and step["reason"].startswith("TIMEOUT") and "monotonic" in step["reason"]
    assert len(fake.calls()) == 1, "no automatic retry"
    for pid in map(int, (fake.dir / "pids").read_text().split()):
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.1)
        else:
            pytest.fail(f"process {pid} survived the timeout")
    [(_, ev)] = _evidence(project)
    assert ev["status"] == "TIMEOUT" and ev["duration_s"] >= 1


# ---- failures and rejected answers --------------------------------------------------------------

@pytest.mark.parametrize("mode,needle", [("error", "reported an error"), ("exit1", "usage limit"), ("garbage", "without a result envelope")])
def test_cli_failures_fall_back_without_retry(project, tmp_path, fake, capsys, mode, needle):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    fake.set(["You've hit your limit"], mode=mode)
    before = conti.conti_path(project, "S001").read_bytes()
    assert _loop(project) == 0
    step = _out(capsys)["driver"]["steps"][0]
    assert step["status"] == "FALLBACK_MANUAL" and needle in step["reason"]
    assert len(fake.calls()) == 1
    assert conti.conti_path(project, "S001").read_bytes() == before


def _doc(project, **changes):
    doc = conti.load_conti(project, "S001")
    doc.update(changes)
    return doc


def _bad_answers(project):
    in_ms = _shot(project, "S001")["in_ms"]
    base = _doc(project)
    outside = [{"at_ms": in_ms + 900_000, "action": "outside the shot"}]
    return {
        "not json": "I think the camera should move more.",
        "other shot": json.dumps({"conti": [{**base, "shot_id": "S002"}]}),
        "two shots": json.dumps({"conti": [base, {**base, "shot_id": "S002"}]}),
        "unchanged": json.dumps({"conti": [base]}),
        "out of range": json.dumps({"conti": [_doc(project, camera={"type": "dolly", "moves": outside})]}),
        "weakened criteria": json.dumps({"conti": [_doc(project, acceptance_criteria=["it looks nice"])]}),
        "unknown key": json.dumps({"conti": [_doc(project, surprise=1)]}),
    }


@pytest.mark.parametrize("name", ["not json", "other shot", "two shots", "unchanged", "out of range", "weakened criteria", "unknown key"])
def test_bad_rewrites_are_rejected_before_anything_is_written(project, tmp_path, fake, capsys, name):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    answer = _bad_answers(project)[name]
    fake.set([answer])
    before = conti.conti_path(project, "S001").read_bytes()
    other = conti.conti_path(project, "S002")
    assert _loop(project) == 0
    row = _out(capsys)
    step = row["driver"]["steps"][0]
    assert step["status"] == "FALLBACK_MANUAL" and "REJECTED" in step["reason"] and "nothing was stored" in step["reason"]
    assert conti.conti_path(project, "S001").read_bytes() == before and not other.exists()
    assert row["stage"] == "AWAITING_CONTI_CHANGE"
    [(path, ev)] = _evidence(project, "rewrite")
    assert ev["status"] == "REJECTED"
    assert (path.parent / ev["answer_file"]).read_text(encoding="utf-8") == answer, "the raw answer is kept even when rejected"


def test_criteria_may_change_when_an_open_question_says_why(project, tmp_path, fake, capsys):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    criteria = ["the red coat is visible", "the cup is set down, not thrown", "the lens moves toward the face"]
    fake.set([_answer(project, acceptance_criteria=criteria, open_questions=["criterion 3 was not checkable from a still"])])
    assert _loop(project) == 0
    assert _out(capsys)["driver"]["steps"][0]["status"] == "APPLIED"
    assert conti.load_conti(project, "S001")["acceptance_criteria"] == criteria


def test_bad_judge_answers_are_rejected(project, tmp_path, fake, capsys):
    _conti(project, "S001")
    take = _take(project, tmp_path, "t1")
    set_production(project, rewrite_driver="claude_cli")
    for answer in (json.dumps({"take": "0" * 64, "criteria": [{"index": 1, "verdict": "PASS"}]}),
                   json.dumps({"take": take["take_sha256"], "criteria": [{"index": 9, "verdict": "PASS"}]}),
                   json.dumps({"take": take["take_sha256"], "criteria": [{"index": 1, "verdict": "GREAT"}]}),
                   "looks fine to me"):
        fake.set([answer])
        assert _loop(project) == 0
        step = _out(capsys)["driver"]["steps"][0]
        assert step["status"] == "FALLBACK_MANUAL" and "REJECTED" in step["reason"], answer
        assert step["manual"]["next"].startswith("paste ") and "conti-verdict" in step["manual"]["next"]
    assert [c["verdict"] for c in conti_brief.load_check(project, "S001")["criteria"]] == ["PENDING"] * 3


def test_cap_and_accepted_shots_make_no_call(project, tmp_path, fake, capsys):
    set_production(project, rewrite_driver="claude_cli", max_attempts_per_shot=1)
    _failed_take(project, tmp_path)
    assert _loop(project) == 0
    row = _out(capsys)
    assert row["stage"] == "CAP_REACHED" and row["driver"]["steps"] == []
    assert fake.calls() == [] and fake.calls("auth") == []


def test_no_driver_flag_only_reports(project, tmp_path, fake, capsys):
    _failed_take(project, tmp_path)
    set_production(project, rewrite_driver="claude_cli")
    assert _loop(project, "--no-driver") == 0
    row = _out(capsys)
    assert row["stage"] == "NEEDS_REWRITE" and "driver" not in row and fake.calls() == []


def test_the_schema_shown_to_the_model_names_every_required_key():
    """Found by the one real-CLI run: a model that follows the example literally omitted
    schema_version, which validate_conti requires. The example must carry it."""
    example = conti.CONTI_SCHEMA["conti"][0]
    assert example["schema_version"] == conti.SCHEMA_VERSION and set(example) <= conti.KEYS
    assert json.dumps(example, ensure_ascii=False) in conti_brief.REWRITE_SYSTEM.format(
        language="English", schema=json.dumps(example, ensure_ascii=False))


@pytest.mark.parametrize("answer", ['{"conti": "S001"}', '{"conti": [3]}', '{"take": 5, "criteria": ["x"]}',
                                    '{"criteria": [{"index": [], "verdict": "PASS"}]}'])
def test_malformed_answers_never_crash_the_loop(project, tmp_path, fake, capsys, answer):
    _conti(project, "S001")
    take = _take(project, tmp_path, "t1")
    set_production(project, rewrite_driver="claude_cli")
    conti_brief.record_verdicts(project, "S001", take["take_sha256"], {3: "FAIL"}, "judge")
    fake.set([answer])
    assert _loop(project) == 0
    steps = _out(capsys)["driver"]["steps"]
    assert steps[0]["status"] == "FALLBACK_MANUAL" and "REJECTED" in steps[0]["reason"], steps
