"""Subscription-CLI driver for the conti revision loop (W00 redesign R6).

``conti-loop`` normally stops at "paste this request to your LLM". With
``project.yaml`` ``production.rewrite_driver: claude_cli`` it answers two of those
requests itself, through the Claude Code CLI the vendor ships with the user's own
subscription (``claude -p``):

* the **judge** request of a checked take (``conti-judge-request``) — the CLI reads
  the sampled frames with its Read tool and answers per-criterion verdicts;
* the **rewrite** request after a FAIL (``conti-rewrite-request``) — the CLI
  answers the shot's revised conti.

Rules this module keeps:

* **No API key, no paid API.** Every ``ANTHROPIC_*`` and ``CLAUDE_CODE_USE_*``
  variable is removed from the child's environment, so a key can never carry the
  call, and before the first call ``claude auth status`` must report a signed-in
  claude.ai (subscription) login. Anything else — not signed in, an API-key or
  cloud-provider login — refuses the call; an ``ANTHROPIC_API_KEY`` that is set is
  reported as ignored, never used.
* **Official interface only.** This drives the vendor's CLI, not a consumer web
  app. Video apps keep the strict no-scripting hand-off rule (``engine/handoff.py``).
* **Bounded, never retried.** One call per request, killed (process group) when a
  ``time.monotonic()`` deadline passes. No polling, no automatic retry; running
  ``conti-loop`` again is a new, explicit request.
* **Evidence first.** The raw CLI output, the extracted answer, the model that
  answered and the outcome are written to ``qc/conti/<shot>/driver/`` before the
  answer is looked at. The answer is validated against the existing schema
  (``validate_conti`` / the judge parser) and only then stored.
* **Fallback is the paste-it-yourself path.** Missing CLI, refusal, timeout, an
  error envelope, a bad or rejected answer: the step reports FALLBACK_MANUAL with
  the persisted request and the next command, and nothing else changes.

The command is configurable outside the project (a project folder must not be
able to choose what gets executed): ``FILM_UNIT_CLAUDE_CMD``, else ``command`` in
the ``claude_cli`` entry of ``providers.json`` under ``FILM_UNIT_HOME``, else
``claude``. Same for ``timeout_seconds`` (``FILM_UNIT_CLAUDE_TIMEOUT``) and
``model``.
"""
from pathlib import Path
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time

from .conti import load_conti, submit_answer, validate_conti
from .conti import _shot as _conti_shot
from .conti_brief import _changed, judge_request, revision_loop, rewrite_request, submit_judgement
from .core import FilmError, atomic_text, now, production_block, safe_path, write
from .director import extract_json
from .settings import get_settings

SCHEMA_VERSION = 1
DRIVER_DIR = "driver"
DEFAULT_COMMAND = "claude"
DEFAULT_TIMEOUT = 600.0
MAX_TIMEOUT = 3600.0
AUTH_TIMEOUT = 30.0
MAX_OUTPUT_BYTES = 4_000_000
SUBSCRIPTION_AUTH = "claude.ai"
SCRUBBED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
SOURCE_LABEL = "llm:claude_cli"
JUDGE_PREFACE = ("Open every frame image listed under the frames heading below with your Read tool before you answer. "
                 "Judge only from what you see in them.\n\n")


class Fallback(Exception):
    """The CLI route cannot answer this request; the person pastes it instead."""

    def __init__(self, status, reason, **extra):
        super().__init__(reason)
        self.status, self.reason, self.extra = status, reason, extra


# ---- configuration --------------------------------------------------------------------

def rewrite_driver(p):
    """project.yaml production.rewrite_driver: 'manual' (default) or 'claude_cli'."""
    value = production_block(p).get("rewrite_driver") or "manual"
    if value not in ("manual", "claude_cli"):
        raise FilmError(f"project.yaml production.rewrite_driver must be manual or claude_cli, not {value!r}")
    return value


def cli_config():
    """Command, timeout and model from the environment, then the user-level settings."""
    saved = get_settings("claude_cli")
    command = os.environ.get("FILM_UNIT_CLAUDE_CMD") or saved.get("command") or DEFAULT_COMMAND
    raw = os.environ.get("FILM_UNIT_CLAUDE_TIMEOUT") or saved.get("timeout_seconds") or DEFAULT_TIMEOUT
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        raise FilmError(f"claude_cli timeout_seconds must be a number, not {raw!r}") from None
    if not 1 <= timeout <= MAX_TIMEOUT:
        raise FilmError(f"claude_cli timeout_seconds must be between 1 and {int(MAX_TIMEOUT)}")
    argv = shlex.split(command, posix=os.name != "nt")
    if not argv:
        raise FilmError("claude_cli command is empty")
    return {"argv": argv, "timeout": timeout, "model": saved.get("model") or None}


def clean_environment(environ=None):
    """The child's environment without any API-key or cloud-provider routing, and the names removed."""
    source = dict(os.environ if environ is None else environ)
    removed = sorted(k for k in source if k.upper().startswith(SCRUBBED_PREFIXES))
    return {k: v for k, v in source.items() if k not in removed}, removed


# ---- running the CLI --------------------------------------------------------------------

def _kill(proc):
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def _run(argv, env, cwd, timeout, stdin_text=None):
    """One bounded run; the deadline is on the monotonic clock so a wall-clock jump cannot extend or cut it."""
    started = time.monotonic()
    deadline = started + timeout
    extra = {"start_new_session": True} if os.name == "posix" else {}
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                                text=True, encoding="utf-8", errors="replace", **extra)
    except OSError as exc:
        raise Fallback("MISSING", f"could not start {argv[0]!r}: {exc}") from None
    try:
        out, err = proc.communicate(stdin_text, timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise Fallback("TIMEOUT", f"no answer within {timeout:g} s (monotonic deadline); the process was killed",
                       duration_s=round(time.monotonic() - started, 2)) from None
    if len(out.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES:
        raise Fallback("FAILED", "the CLI output is larger than the 4 MB limit")
    return {"exit_code": proc.returncode, "stdout": out, "stderr": err[-2000:],
            "duration_s": round(time.monotonic() - started, 2)}


class ClaudeCli:
    """`claude -p` on the user's subscription login. One preflight per object; no retries."""

    def __init__(self, argv, timeout, model=None, environ=None):
        self.argv, self.timeout, self.model = list(argv), timeout, model
        self.env, self.ignored_env = clean_environment(environ)
        self._auth = None

    @classmethod
    def from_settings(cls):
        return cls(**cli_config())

    def available(self):
        if shutil.which(self.argv[0], path=self.env.get("PATH")) is None:
            raise Fallback("MISSING", f"the Claude Code CLI ({self.argv[0]!r}) is not on PATH")

    def preflight(self):
        """Fail closed unless the CLI reports a signed-in claude.ai subscription login (memoized)."""
        if self._auth is not None:
            if isinstance(self._auth, Fallback):
                raise self._auth
            return self._auth
        try:
            self.available()
            result = _run([*self.argv, "auth", "status", "--json"], self.env, tempfile.gettempdir(), AUTH_TIMEOUT)
            try:
                info = json.loads(result["stdout"])
            except ValueError:
                info = None
            if result["exit_code"] != 0 or not isinstance(info, dict):
                raise Fallback("REFUSED", "`claude auth status` did not report a login; run `claude auth login` "
                               "with the subscription account")
            method, provider = info.get("authMethod"), info.get("apiProvider", "firstParty")
            if not info.get("loggedIn") or method != SUBSCRIPTION_AUTH or provider != "firstParty":
                note = ("; ANTHROPIC_API_KEY is set but FILM UNIT never uses API keys" if "ANTHROPIC_API_KEY" in self.ignored_env else "")
                raise Fallback("REFUSED", f"no Claude subscription login (authMethod {method!r}, loggedIn "
                               f"{bool(info.get('loggedIn'))}): only a claude.ai subscription login may answer{note}")
            self._auth = {"authMethod": method, "apiProvider": provider, "subscriptionType": info.get("subscriptionType")}
        except Fallback as exc:
            self._auth = exc
            raise
        return self._auth

    def ask(self, prompt, tools="", add_dirs=()):
        """Send the prompt on stdin; returns the answer text, the model and the raw output."""
        auth = self.preflight()
        argv = [*self.argv, "-p", "--output-format", "json", "--no-session-persistence", "--tools", tools]
        if tools:
            argv += ["--allowedTools", tools]
        if self.model:
            argv += ["--model", self.model]
        for folder in add_dirs:
            argv += ["--add-dir", str(folder)]
        with tempfile.TemporaryDirectory(prefix="filmunit-cli-") as cwd:
            result = _run(argv, self.env, cwd, self.timeout, stdin_text=prompt)
        raw = result["stdout"]
        try:
            envelope = json.loads(raw)
        except ValueError:
            envelope = None
        base = {"argv": argv, "raw": raw, "exit_code": result["exit_code"], "duration_s": result["duration_s"],
                "stderr": result["stderr"], "auth": auth}
        if result["exit_code"] != 0 or not isinstance(envelope, dict) or envelope.get("type") != "result":
            raise Fallback("FAILED", f"the CLI exited {result['exit_code']} without a result envelope: "
                           f"{(result['stderr'] or raw)[-300:].strip()}", **base)
        usage = envelope.get("modelUsage") if isinstance(envelope.get("modelUsage"), dict) else {}
        models = sorted(usage, key=lambda m: -((usage[m].get("outputTokens") or 0) if isinstance(usage[m], dict) else 0))
        text = envelope.get("result")
        base.update(models=models, model=models[0] if models else None)
        if envelope.get("is_error") or not isinstance(text, str) or not text.strip():
            raise Fallback("FAILED", f"the CLI reported an error: {str(text or envelope.get('subtype'))[:300]}", **base)
        return {**base, "answer": text}


# ---- evidence ---------------------------------------------------------------------------

def _evidence(p, shot_id, kind, request_id):
    """Reserve a unique stem under qc/conti/<shot>/driver/; nothing is overwritten."""
    folder = safe_path(p, f"qc/conti/{shot_id}/{DRIVER_DIR}")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    for n in range(1, 1000):
        stem = folder / f"{stamp}-{kind}-{request_id[:16]}-{n:02d}"
        try:
            stem.with_suffix(".json").touch(exist_ok=False)
            return stem
        except FileExistsError:
            continue
    raise FilmError("could not reserve an evidence file name")


def _write_evidence(p, stem, shot_id, kind, request, status, reason, prompt, outcome=None, **extra):
    """The evidence record, plus the raw output and the extracted answer beside it."""
    record = {"schema_version": SCHEMA_VERSION, "shot_id": shot_id, "kind": kind, "driver": "claude_cli",
              "request_id": request["id"], "request_file": request["file"],
              "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
              "status": status, "reason": reason, "at": now()}
    if outcome:
        record.update(model=outcome.get("model"), models=outcome.get("models", []),
                      argv=outcome.get("argv", []), exit_code=outcome.get("exit_code"),
                      duration_s=outcome.get("duration_s"), auth=outcome.get("auth"))
        if outcome.get("raw"):
            atomic_text(stem.with_suffix(".stdout.txt"), outcome["raw"])
            record["raw_output_file"] = stem.with_suffix(".stdout.txt").name
        if outcome.get("answer") is not None:
            atomic_text(stem.with_suffix(".answer.txt"), outcome["answer"])
            record["answer_file"] = stem.with_suffix(".answer.txt").name
    record.update({k: v for k, v in extra.items() if v is not None})
    write(stem.with_suffix(".json"), record)
    return str(stem.with_suffix(".json").relative_to(p))


# ---- the two steps -----------------------------------------------------------------------

def _manual(request, evidence, status, reason, next_step):
    return {"status": "FALLBACK_MANUAL", "reason": f"{status}: {reason}", "evidence": evidence,
            "manual": {"request_id": request["id"], "request_file": request["file"], "next": next_step}}


def _drive(p, shot_id, kind, request, prompt, cli, tools, add_dirs, store, next_step):
    """Run one request through the CLI and store the validated answer; any failure is the manual fallback."""
    stem = _evidence(p, shot_id, kind, request["id"])
    outcome = {}
    try:
        outcome = cli.ask(prompt, tools=tools, add_dirs=add_dirs)
    except Fallback as exc:
        outcome = dict(exc.extra)
        evidence = _write_evidence(p, stem, shot_id, kind, request, exc.status, exc.reason, prompt, outcome,
                                   ignored_env=cli.ignored_env or None)
        return _manual(request, evidence, exc.status, exc.reason, next_step)
    try:
        summary = store(outcome["answer"])
    except (FilmError, KeyError, TypeError, ValueError, AttributeError) as exc:    # a malformed answer must not stop the loop
        reason = f"the answer was rejected and nothing was stored: {exc}"
        evidence = _write_evidence(p, stem, shot_id, kind, request, "REJECTED", reason, prompt, outcome,
                                   ignored_env=cli.ignored_env or None)
        return _manual(request, evidence, "REJECTED", reason, next_step)
    evidence = _write_evidence(p, stem, shot_id, kind, request, "APPLIED", "answer validated and stored", prompt,
                               outcome, ignored_env=cli.ignored_env or None)
    return {"status": "APPLIED", "model": outcome.get("model"), "evidence": evidence, **summary}


def _store_rewrite(p, shot_id, request_id, answer):
    """Validate the rewrite against the conti schema and the loop's rules, then store it."""
    data = extract_json(answer)
    docs = data.get("conti") if "conti" in data else [data]
    if not isinstance(docs, list) or len(docs) != 1 or not isinstance(docs[0], dict) or docs[0].get("shot_id") != shot_id:
        raise FilmError(f"the answer must be exactly one conti document for {shot_id}")
    before = load_conti(p, shot_id)
    clean = validate_conti(_conti_shot(p, shot_id), docs[0])
    changed = _changed(before, clean)
    if not changed:
        raise FilmError("the rewrite changes nothing in the conti")
    if clean["acceptance_criteria"] != before["acceptance_criteria"] and clean["open_questions"] == before["open_questions"]:
        raise FilmError("acceptance_criteria changed without an open_question saying why; criteria are not weakened silently")
    rows = submit_answer(p, {"conti": [docs[0]]}, source=SOURCE_LABEL, request_id=request_id)
    return {"conti_changed": changed, "ready": rows[0]["ready"], "missing": rows[0]["missing"]}


def rewrite_step(project, shot_id, cli, language="English"):
    """Answer the shot's rewrite request through the CLI. Returns the step row."""
    p = Path(project)
    try:
        request = rewrite_request(p, shot_id, language=language)
    except FilmError as exc:
        return {"status": "SKIPPED", "reason": str(exc)}
    asked = {"id": request["request_id"], "file": request["request_file"]}
    next_step = request["next"]
    return _drive(p, shot_id, "rewrite", asked, request["text"], cli, "", (),
                  lambda answer: _store_rewrite(p, shot_id, asked["id"], answer), next_step)


def judge_step(project, shot_id, sha256, cli):
    """Answer the take's judge request through the CLI (the CLI reads the frames). Returns the step row."""
    p = Path(project)
    try:
        request = judge_request(p, shot_id, sha256)
    except FilmError as exc:
        return {"status": "SKIPPED", "reason": str(exc)}
    asked = {"id": Path(request["request_file"]).stem.rpartition("judge-")[2], "file": request["request_file"]}
    frames = {str(Path(f).parent) for f in request["attach"]}

    def store(answer):
        record = submit_judgement(p, shot_id, answer, sha256=request["take"], source=SOURCE_LABEL)
        return {"verdict": record["verdict"],
                "criteria": {str(c["index"]): c["verdict"] for c in record["criteria"]},
                "unsure": [c["index"] for c in record["criteria"] if c["verdict"] == "PENDING"]}

    return _drive(p, shot_id, "judge", asked, JUDGE_PREFACE + request["text"], cli, "Read", sorted(frames), store,
                  f"paste {request['request_file']} (and attach the frames) to your LLM, then conti-judge-submit "
                  f"{shot_id} --file <answer> — or settle the criteria yourself with conti-verdict")


def advance(project, shot_id, cli=None, language="English"):
    """One pass over a shot: judge a take whose criteria are pending, rewrite after a FAIL.

    The pass ends when the loop needs a person (a new take, UNSURE criteria, the
    attempt cap) or a step fell back to the manual path. It never repeats a step.
    With ``rewrite_driver: manual`` it only reports the loop stage, as before.
    """
    p = Path(project)
    loop = revision_loop(p, shot_id)
    if rewrite_driver(p) != "claude_cli":
        return loop
    cli = cli or ClaudeCli.from_settings()
    steps = []
    if loop["stage"] == "NEEDS_VERDICTS":
        step = {"step": "judge", **judge_step(p, shot_id, loop.get("take_sha256"), cli)}
        steps.append(step)
        if step["status"] == "APPLIED":
            loop = revision_loop(p, shot_id)
    if loop["stage"] in ("NEEDS_REWRITE", "AWAITING_CONTI_CHANGE") and not any(s["status"] == "FALLBACK_MANUAL" for s in steps):
        steps.append({"step": "rewrite", **rewrite_step(p, shot_id, cli, language=language)})
        loop = revision_loop(p, shot_id)
    return {**loop, "driver": {"mode": "claude_cli", "steps": steps}}
