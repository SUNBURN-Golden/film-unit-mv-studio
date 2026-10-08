"""film-workspace-accessibility — production workspace guide.

Plain-language next step for 준비 → 동작 → 검토 → 수정 → 출력, plus a
keyboard/number/zoom model for a long integer-frame timeline and the
empty, missing-credential, error and restart screens.

The guide reads the shot board, the frame clock and (when present) the
execution journal. It never writes lyrics, cues, audio, reviews or locks,
and it never starts OAuth, a network call or a retry. A refused save
leaves the previous selected frame and note on disk.

Preferences live in ``workspace/accessibility.json`` as canonical JSON
written by this module. The document type is local to the workspace
screen — it is not part of the locked animation schema registry.
Qualification stays UNQUALIFIED, acceptance PENDING, release
NOT_AUTHORIZED. Preview and this screen are not a Final.
"""
import os
from pathlib import Path

from .animation_schema import (read_canon, require_animation_profile,
                               write_canon)
from .core import FilmError, project_mutex
from .drive_oauth import SECRET_FIELDS, load_connection_metadata
from .durable_journal import TAIL_CLEAN, load_journal
from .shot_board import board_view

DOC_TYPE = "workspace_accessibility"
SCHEMA_VERSION = 1
REL = "workspace/accessibility.json"
NOTE_LIMIT = 2000
DISPLAY_CAP = 24
JOURNAL_REL = Path("render") / "execution" / "job_journal.jsonl"

# Wider first. "확대" walks toward a single frame.
ZOOM_ORDER = ("wide", "span", "second", "close", "frame")
ZOOM_WINDOWS = {"wide": 480, "span": 96, "second": 24, "close": 8,
                "frame": 1}

FACETS = {"qualification_state": "UNQUALIFIED",
          "acceptance_state": "PENDING",
          "release_state": "NOT_AUTHORIZED"}

# What a person can finish from the screen without opening a JSON file.
CORE_ACTIONS = (
    "다음 단계 안내를 읽는다",
    "프레임 번호를 고른다",
    "타임라인을 확대하거나 축소한다",
    "동작 줄이기를 켠다",
    "메모를 남긴다",
    "재시작 후 같은 작업을 확인한다",
)

STAGE_META = (
    ("prepare", "준비",
     "그림과 컷 순서를 갖춥니다. 자산 화면에서 그림 묶음을 넣습니다."),
    ("motion", "동작",
     "컷이 어떻게 움직이는지 계획을 남깁니다. 애니메이션 화면에서 저장합니다."),
    ("review", "검토",
     "지금 그림이 맞는지 사람이 확인합니다. 맞다와 고칠 점은 글자로 남깁니다."),
    ("revise", "수정",
     "지적된 컷만 고치고 다시 확인합니다. 예전 검수와 잠금은 새 그림에 따라가지 않습니다."),
    ("output", "출력",
     "잠금, 경로 결정, 최종 후보, 최종 승인은 각각 따로 기록합니다. 미리보기는 완성본이 아닙니다."),
)

_PASSED = {
    "prepare": frozenset(),
    "motion": frozenset({"prepare"}),
    "review": frozenset({"prepare", "motion"}),
    "revise": frozenset({"prepare", "motion"}),
    "output": frozenset({"prepare", "motion", "review", "revise"}),
}

_REVISE = frozenset({"STALE", "CHANGES_REQUIRED", "UNRESOLVED"})
_OPEN_EVENTS = frozenset({
    "SUBMIT_INTENT", "SUBMIT_LOST", "CANCEL_LOST", "UPLOAD_INTENT",
    "UPLOAD_SESSION_LOST", "COMMIT_INTENT", "MANIFEST_INTENT",
    "SEAL_UNKNOWN"})

_FILE_FIELDS = {"document_type", "schema_version", "selected_frame", "zoom",
                "reduced_motion", "narrow_layout", "draft_note", "resume_ack"}

_MARKERS = {
    "CURRENT": ("검수 완료", "✓", "solid"),
    "UNREVIEWED": ("미검수", "○", "open"),
    "STALE": ("재검수", "↻", "return"),
    "CHANGES_REQUIRED": ("수정 필요", "!", "bang"),
    "UNRESOLVED": ("미해결", "!", "bang"),
    "FAILED": ("실패", "✕", "cross"),
    "ERROR": ("오류", "✕", "cross"),
    "IN_PROGRESS": ("진행 중", "…", "dots"),
    "PENDING": ("대기", "–", "dash"),
    "DONE": ("마침", "✓", "solid"),
    "UNLOCKED": ("잠금 없음", "○", "open"),
    "EMPTY": ("비어 있음", "∅", "empty"),
    "MISSING_CREDENTIAL": ("자격 없음", "⚿", "key"),
    "RESTART": ("이어서", "↩", "return"),
    "NONE": ("해당 없음", "–", "dash"),
    "NOT_AUTHORIZED": ("승인 아님", "✗", "cross"),
    "UNQUALIFIED": ("미자격", "○", "open"),
}

_KEPT = "저장된 선택 프레임과 메모는 그대로입니다."


def marker(code):
    """Word plus a shape. Status is never a color by itself."""
    word, mark, pattern = _MARKERS.get(code, (str(code), "•", "dot"))
    return {"code": code, "word": word, "mark": mark, "pattern": pattern,
            "text": f"{mark} {word}"}


def default_prefs():
    return {"document_type": DOC_TYPE, "schema_version": SCHEMA_VERSION,
            "selected_frame": 0, "zoom": "second", "reduced_motion": False,
            "narrow_layout": False, "draft_note": "", "resume_ack": False,
            "stored": False}


def _prefs_path(project):
    return Path(project) / REL


def _check_prefs(document):
    if type(document) is not dict or set(document) != _FILE_FIELDS:
        raise FilmError("작업실 설정 형식이 아닙니다. " + _KEPT)
    if document["document_type"] != DOC_TYPE:
        raise FilmError("작업실 설정 종류가 아닙니다. " + _KEPT)
    if type(document["schema_version"]) is not int \
            or document["schema_version"] != SCHEMA_VERSION:
        raise FilmError("작업실 설정 버전을 읽을 수 없습니다. " + _KEPT)
    if type(document["selected_frame"]) is not int \
            or document["selected_frame"] < 0:
        raise FilmError("저장된 프레임 번호가 정수가 아닙니다. " + _KEPT)
    if document["zoom"] not in ZOOM_WINDOWS:
        raise FilmError("저장된 확대 단계를 읽을 수 없습니다. " + _KEPT)
    for key in ("reduced_motion", "narrow_layout", "resume_ack"):
        if type(document[key]) is not bool:
            raise FilmError(f"저장된 {key} 값이 올바르지 않습니다. " + _KEPT)
    note = document["draft_note"]
    if type(note) is not str or "\x00" in note or len(note) > NOTE_LIMIT:
        raise FilmError("저장된 메모를 읽을 수 없습니다. " + _KEPT)
    return document


def load_prefs(project):
    """Read the workspace note. Missing file → defaults. Never deletes."""
    p = Path(project)
    require_animation_profile(p)
    path = _prefs_path(p)
    if not path.is_file():
        return default_prefs()
    document = _check_prefs(read_canon(path))
    return {**document, "stored": True}


def _validate_shape(frame, zoom, reduced_motion, narrow_layout, note,
                    resume_ack):
    if type(frame) is not int:
        raise FilmError("프레임 번호는 정수여야 합니다. " + _KEPT)
    if zoom not in ZOOM_WINDOWS:
        raise FilmError("확대 단계를 알 수 없습니다. " + _KEPT)
    if type(reduced_motion) is not bool or type(narrow_layout) is not bool \
            or type(resume_ack) is not bool:
        raise FilmError("설정 값이 올바르지 않습니다. " + _KEPT)
    if type(note) is not str or "\x00" in note or len(note) > NOTE_LIMIT:
        raise FilmError(f"메모는 {NOTE_LIMIT}자까지입니다. " + _KEPT)


def commit_workspace(project, *, frame, zoom, reduced_motion, narrow_layout,
                     note, resume_ack=False):
    """Validate, then write. A refusal leaves the previous file untouched.

    Lyrics, cues, audio, the timeline, reviews and locks are not written.
    """
    p = Path(project)
    require_animation_profile(p)
    _validate_shape(frame, zoom, reduced_motion, narrow_layout, note,
                    resume_ack)
    with project_mutex(p):
        config = require_animation_profile(p)
        output = config["animation"]["output_frames"]
        if type(output) is not int or output < 1:
            raise FilmError("타임라인 길이를 알 수 없습니다. " + _KEPT)
        if not 0 <= frame < output:
            raise FilmError(
                f"프레임은 0 이상 {output - 1} 이하여야 합니다. " + _KEPT)
        document = {
            "document_type": DOC_TYPE, "schema_version": SCHEMA_VERSION,
            "selected_frame": frame, "zoom": zoom,
            "reduced_motion": reduced_motion,
            "narrow_layout": narrow_layout, "draft_note": note,
            "resume_ack": resume_ack}
        write_canon(_prefs_path(p), document)
    return load_prefs(p)


def _label(row):
    if row.get("shot_id"):
        return row["shot_id"]
    if row.get("id"):
        return f"전환 {row['id']}"
    return row.get("instance_id") or "?"


def _names(rows, limit=4):
    labels = [_label(row) for row in rows]
    if len(labels) <= limit:
        return ", ".join(labels)
    return ", ".join(labels[:limit]) + f" 외 {len(labels) - limit}컷"


def _subject(rows):
    shots = any(row.get("shot_id") for row in rows)
    transitions = any(row.get("id") and not row.get("shot_id") for row in rows)
    if shots and transitions:
        return "컷·전환"
    if transitions:
        return "전환"
    return "컷"


def _review_state(row):
    state = row.get("review")
    return state if state else "UNREVIEWED"


def decide_stage(rows, locks=None, transitions=None):
    """Pick the single next stage from board rows. Does not write.

    Transition reviews count with the cuts. An unreviewed or stale
    transition does not leave the guide on 출력.
    """
    rows = list(rows or [])
    transitions = list(transitions or [])
    locks = locks or {}
    unpinned = [row for row in rows if not (row.get("pin") or {}).get("resolved")]
    unplanned = [row for row in rows
                 if not (isinstance(row.get("plan"), dict)
                         and "error" not in row["plan"])]
    revising = [row for row in rows if _review_state(row) in _REVISE]
    revising += [row for row in transitions if _review_state(row) in _REVISE]
    unreviewed = [row for row in rows if _review_state(row) != "CURRENT"]
    unreviewed += [row for row in transitions
                   if _review_state(row) != "CURRENT"]
    work_empty = not rows or bool(unpinned)
    if not rows or unpinned:
        current = "prepare"
        nxt = ("다음은 준비입니다. 타임라인에 컷이 없습니다."
               if not rows else
               f"다음은 준비입니다. 그림이 없는 컷: {_names(unpinned)}. "
               "자산 화면에서 그림 묶음을 넣으세요.")
    elif unplanned:
        current = "motion"
        nxt = (f"다음은 동작입니다. 움직임 계획이 없는 컷: {_names(unplanned)}. "
               "애니메이션 화면에서 계획을 저장하세요.")
    elif revising:
        current = "revise"
        fix = "그 컷만" if _subject(revising) == "컷" else "그 대상만"
        nxt = (f"다음은 수정입니다. 다시 손볼 {_subject(revising)}: "
               f"{_names(revising)}. "
               f"{fix} 고친 뒤 다시 확인하세요. "
               "예전 확인은 새 그림에 따라가지 않습니다.")
    elif unreviewed:
        current = "review"
        nxt = (f"다음은 검토입니다. 아직 확인 전인 {_subject(unreviewed)}: "
               f"{_names(unreviewed)}. "
               "검수 화면에서 맞다 또는 고칠 점을 남기세요.")
    else:
        current = "output"
        final = locks.get("final")
        if final == "CURRENT":
            tail = " 최종 잠금 기록이 현재 내용과 같습니다. 그래도 작품 승인은 아닙니다."
        else:
            tail = " 최종 잠금은 아직 없습니다."
        nxt = ("다음은 출력입니다. 잠금, 경로 결정, 최종 후보, 최종 승인은 "
               "각각 따로 기록합니다. 이 화면은 승인을 대신하지 않습니다."
               + tail)
    help_text = {stage_id: text for stage_id, _label, text in STAGE_META}[current]
    stages = []
    passed = _PASSED[current]
    for stage_id, label, stage_help in STAGE_META:
        if stage_id == "revise" and current == "output":
            status = marker("NONE")
        elif stage_id == current:
            status = marker("IN_PROGRESS")
        elif stage_id in passed:
            status = marker("DONE")
        else:
            status = marker("PENDING")
        stages.append({"id": stage_id, "label": label, "help": stage_help,
                       "status": status})
    return {"current": current, "next_action": nxt, "help": help_text,
            "stages": stages, "work_empty": work_empty,
            "json_edit_required": False, "core_actions": list(CORE_ACTIONS)}


def journey(project):
    """Next action for one FRAME_ANIMATION_V1 project. LEGACY_MV raises."""
    view = board_view(project)
    report = decide_stage(view["entries"], {
        "final": (view.get("locks") or {}).get("final"),
        "plan": (view.get("locks") or {}).get("plan")},
        view.get("transitions"))
    entries = [{
        "instance_id": row["instance_id"], "shot_id": row["shot_id"],
        "output_range": list(row["output_range"]),
        "review": row.get("review") or "UNREVIEWED",
    } for row in view["entries"]]
    transitions = [{
        "output_range": list(item["output_range"]),
        "to_instance": item["to_instance"],
        "review": item.get("review") or "UNREVIEWED",
    } for item in view["transitions"]]
    fps = view["fps"]
    output_frames = view["output_frames"]
    report.update({
        "fps": fps, "output_frames": output_frames, "entries": entries,
        "transitions": transitions,
        "baseline_note": (
            "프로그램 기준은 240초 · 24fps (5760프레임) 입니다. "
            f"이 프로젝트는 {output_frames}프레임 · {fps}fps 입니다."),
        "legend": [marker("CURRENT"), marker("FAILED"),
                   marker("IN_PROGRESS"), marker("UNREVIEWED")],
        "facets": dict(FACETS),
        "timeline_sha256": view.get("timeline_sha256")})
    return report


def clamp_frame(total, frame):
    """Nearest index the screen can show. Does not write the saved frame."""
    if type(total) is not int or total < 1:
        raise FilmError("타임라인 길이가 없습니다.")
    if type(frame) is not int or isinstance(frame, bool):
        raise FilmError("프레임 번호는 정수여야 합니다.")
    if frame < 0:
        return 0
    if frame >= total:
        return total - 1
    return frame


def window_bounds(total, frame, zoom):
    """Inclusive-exclusive frame window centered on ``frame``."""
    if type(total) is not int or total < 1:
        raise FilmError("타임라인 길이가 없습니다.")
    if type(frame) is not int or not 0 <= frame < total:
        raise FilmError(f"프레임은 0 이상 {total - 1} 이하여야 합니다.")
    if zoom not in ZOOM_WINDOWS:
        raise FilmError("확대 단계를 알 수 없습니다.")
    span = min(ZOOM_WINDOWS[zoom], total)
    start = frame - span // 2
    if start < 0:
        start = 0
    end = start + span
    if end > total:
        end = total
        start = end - span
    return {"start": start, "end": end, "span": span, "frame": frame,
            "total": total, "zoom": zoom}


def keyboard_targets(total, frame, zoom):
    """Where each keyboard control lands. Reduced motion does not move frames."""
    bounds = window_bounds(total, frame, zoom)
    page = bounds["span"]

    def clamp(value):
        return max(0, min(total - 1, value))

    return {"previous_frame": clamp(frame - 1),
            "next_frame": clamp(frame + 1),
            "previous_page": clamp(frame - page),
            "next_page": clamp(frame + page),
            "home": 0, "end": total - 1}


def zoom_step(zoom, direction):
    if zoom not in ZOOM_WINDOWS:
        raise FilmError("확대 단계를 알 수 없습니다.")
    index = ZOOM_ORDER.index(zoom)
    if direction == "in":
        index = min(len(ZOOM_ORDER) - 1, index + 1)
    elif direction == "out":
        index = max(0, index - 1)
    else:
        raise FilmError("확대 방향을 알 수 없습니다.")
    return ZOOM_ORDER[index]


def _owner(guide, frame_index):
    for transition in guide.get("transitions") or []:
        start, end = transition["output_range"]
        if start <= frame_index < end and end > start:
            return transition["to_instance"]
    for entry in guide.get("entries") or []:
        start, end = entry["output_range"]
        if start <= frame_index < end:
            return entry["instance_id"]
    return None


def timeline_focus(guide, frame, zoom):
    """Rows around the focused frame. The table is capped; the window is not.

    ``reduced_motion`` is intentionally absent: it never changes which
    frame a key lands on.
    """
    bounds = window_bounds(guide["output_frames"], frame, zoom)
    by_id = {entry["instance_id"]: entry for entry in guide["entries"]}
    rows = []
    for index in range(bounds["start"], bounds["end"]):
        owner = _owner(guide, index)
        entry = by_id.get(owner) or {}
        status = marker(entry.get("review") or "UNREVIEWED")
        rows.append({
            "frame_index": index,
            "display": index + 1,
            "file": f"F_{index + 1:06d}.png",
            "owner": owner,
            "shot_id": entry.get("shot_id"),
            "selected": index == frame,
            "selection": "▶ 선택" if index == frame else "",
            "status": status["text"]})
    if len(rows) > DISPLAY_CAP:
        pos = frame - bounds["start"]
        half = DISPLAY_CAP // 2
        origin = max(0, min(pos - half, len(rows) - DISPLAY_CAP))
        shown = rows[origin:origin + DISPLAY_CAP]
    else:
        shown = rows
    return {**bounds, "rows": shown, "window_rows": bounds["end"] - bounds["start"],
            "moves": keyboard_targets(guide["output_frames"], frame, zoom),
            "reduced_motion_changes_frames": False}


def credential_view(home=None):
    """Report a missing real credential. Never reads or returns a secret."""
    if home is None:
        home = os.environ.get("FILM_UNIT_HOME")
    # Do not create ~/.film_unit. An unset home is simply "no connection".
    meta = None
    unreadable = False
    if home:
        try:
            meta = load_connection_metadata(Path(home))
        except (OSError, ValueError, FilmError):
            # JSONDecodeError is a ValueError and may quote the file.
            # Keep that text out of the screen.
            unreadable = True
    record = "읽지 못함" if unreadable else "없음"
    epoch = None
    named_secret = False
    if type(meta) is dict:
        record = "있음"
        if type(meta.get("credential_epoch")) is int:
            epoch = meta["credential_epoch"]
        named_secret = any(key in meta for key in SECRET_FIELDS)
    detail = ("발급된 Google OAuth 클라이언트가 없습니다. "
              "이 화면은 로그인이나 네트워크 연결을 시도하지 않습니다. "
              "실제 저장소 연결은 UNQUALIFIED 입니다.")
    if record == "있음":
        detail += " 저장된 것은 비밀이 아닌 연결 기록뿐이며 토큰 값은 읽지 않습니다."
    if named_secret:
        detail += " 연결 기록에 비밀 필드 이름이 있어 값은 표시하지 않습니다."
    if unreadable:
        detail += " 연결 기록을 읽지 못했습니다. 파일 내용은 표시하지 않습니다."
    return {"state": "MISSING_CREDENTIAL", **marker("MISSING_CREDENTIAL"),
            "connection_record": record, "credential_epoch": epoch,
            "detail": detail, "qualification_state": "UNQUALIFIED",
            "secrets_displayed": False, "auto_retry": False}


def restart_view(project):
    """Read the project execution journal. Never appends and never retries."""
    path = Path(project) / JOURNAL_REL
    base = {"path": str(path), "auto_retry": False,
            "qualification_state": "UNQUALIFIED"}
    if not path.is_file():
        return {**base, "state": "NONE", **marker("NONE"), "tail": None,
                "detail": "실행 기록이 없습니다. 재시작으로 다시 제출하지 않습니다."}
    loaded = load_journal(path)
    if loaded["tail"] != TAIL_CLEAN:
        return {**base, "state": "RESTART", **marker("RESTART"),
                "tail": loaded["tail"],
                "detail": ("기록이 잘렸거나 손상되었습니다. "
                           "자동으로 다시 보내지 않습니다. "
                           "실행 작업실에서 같은 작업을 확인하세요.")}
    records = loaded["records"]
    if not records:
        return {**base, "state": "NONE", **marker("NONE"),
                "tail": loaded["tail"],
                "detail": "실행 기록이 비어 있습니다. 재시작으로 다시 제출하지 않습니다."}
    last = records[-1]
    data = last.get("data") if type(last.get("data")) is dict else {}
    open_ended = (last.get("event") in _OPEN_EVENTS
                  or data.get("to") == "UNKNOWN"
                  or data.get("state") == "UNKNOWN")
    if open_ended:
        return {**base, "state": "RESTART", **marker("RESTART"),
                "tail": loaded["tail"], "event": last.get("event"),
                "detail": (f"마지막 기록은 {last.get('event')} 입니다. "
                           "같은 작업을 확인하고, 새 제출은 하지 않습니다.")}
    return {**base, "state": "NONE", **marker("NONE"),
            "tail": loaded["tail"], "event": last.get("event"),
            "detail": "실행 기록은 닫혀 있습니다. 재시작 대기가 없습니다."}
