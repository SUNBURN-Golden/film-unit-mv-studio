"""film-lyrics-review — 실제 청취와 연결된 가사·한글 자막 검토.

The `film-lyrics-review` node: a lyric/cue review workbench that explains
repeat sections, simultaneous (overlapping or back-to-back) cues, missing
rows, long lines and font coverage; connects a listening position to the
cue it lands on; and names the partial re-review / LOCK invalidation a
subtitle change requires — for both LEGACY_MV and FRAME_ANIMATION_V1
projects, since the lyric contract is shared.

Everything shown here is read back from the existing lyric validators,
fingerprint functions and (in the new profile) the ANIM-020 change-closure
planner — never re-derived independently:

- source rows, repeat suggestions, effective rows and unresolved coverage
  come from `engine.lyrics` (`_source`, `prepare_lyrics`, `_check`);
- review validity is `validate_lyrics`' own verdict bound to
  `lyrics_review_fingerprint` — an imported or ASR candidate document is
  evaluated without writing and never counts as human review;
- glyph coverage reuses `engine.lyrics._font`, the same cmap check Final
  gates on — a failed check is reported, never softened;
- the invalidation projection for cue/font changes is ANIM-020's
  `plan_rebuild` (CUE/FONT change classes): the clean sequence and cut
  reviews stay bound, only the subbed delivery re-encodes;
- clean and subbed masters are read as separate sealed artifacts — never
  one shared hash.

The line-length numbers are reading aids for a human reviewer (font
advance widths when the resolved font file is readable, an explicit
worst-case estimate otherwise); they are advisory, not a gate. All
records here are synthetic protocol records: qualification UNQUALIFIED,
acceptance PENDING, release NOT_AUTHORIZED.
"""
from pathlib import Path
import difflib
import json
import math
import re

from .core import (FilmError, frame_at, production_profile,
                   read, safe_path, timecode)
from .lyrics import (_check, _effective_rows, _font, _source, prepare_lyrics,
                     lyrics_review_fingerprint, timing_fingerprint,
                     validate_lyrics, REVIEW_SCHEMA_VERSION, SCHEMA_VERSION)

FACETS = {"qualification_state": "UNQUALIFIED",
          "acceptance_state": "PENDING",
          "release_state": "NOT_AUTHORIZED"}

# Advisory reading aids for the human reviewer — not a contract gate.
READING_CPS = 20.0         # chars/second guide above which a cue is FAST_READ
GUIDE_LINES = 2            # on-screen line guide above which a cue is LONG_LINE
MIN_CUE_MS = 500           # sub-half-second cues are flagged SHORT

CHANGE_KINDS = ("TIMING", "SUBTITLE_SETTINGS", "FONT", "SOURCE_TEXT")
POSITION_STATES = ("ON_CUE", "IN_GAP", "BEFORE_FIRST", "AFTER_LAST")

_FONT = re.compile(r"U\+([0-9A-F]{4,6})")


def _config(p):
    return read(Path(p) / "project.yaml")


def _duration(p):
    analysis = read(Path(p) / "analysis/audio.json", {}) or {}
    duration = analysis.get("duration_ms")
    return duration if type(duration) is int and duration > 0 else None


def _safe_area(config):
    """The same usable text width `export_subtitles` burns against."""
    fmt, settings = config.get("format", {}), config.get("subtitles", {})
    width, height = int(fmt.get("width", 1440)), int(fmt.get("height", 1080))
    horizontal = max(round(width * 0.05), int(settings.get("margin_x", 0)))
    bottom = max(round(height * 0.07), int(settings.get("margin_bottom", 0)))
    size = float(settings.get("font_size", max(8, round(height * 0.044))))
    return {"width": width, "height": height,
            "margin_x": horizontal, "margin_bottom": bottom,
            "usable_width_px": width - 2 * horizontal,
            "font_size": size}


def _text_width(font_file, text, font_size):
    """Advance-width sum in the resolved font, or None when unmeasurable.

    fontTools is already a declared dependency (the cmap check in
    `engine.lyrics._font` uses it). Characters missing from the cmap are
    counted at the full advance — they are reported separately as missing
    glyphs anyway.
    """
    try:
        from fontTools.ttLib import TTFont
        with TTFont(font_file, fontNumber=0) as font:
            units = font["head"].unitsPerEm
            hmtx = font["hmtx"]
            cmap = font.getBestCmap() or {}
            width_units = 0
            for char in text:
                glyph = cmap.get(ord(char))
                # A missing glyph measures at a full em — conservative for
                # CJK text, and it is flagged separately as missing anyway.
                width_units += hmtx[glyph][0] if glyph else units
            return int(round(width_units * font_size / units))
    except Exception:
        return None


def _cue_rows(document, fps, font_file, area):
    """One display row per cue: text, ms + integer-frame boundaries, the
    reading metrics and the advisory flags."""
    rows = []
    for index, cue in enumerate(document.get("cues", [])):
        start, end = cue["start_ms"], cue["end_ms"]
        text = cue["text"]
        duration = end - start
        cps = round(len(text) / (duration / 1000), 1) if duration else None
        measured = _text_width(font_file, text, area["font_size"]) \
            if font_file else None
        estimate = int(round(len(text) * area["font_size"]))
        width_px = measured if measured is not None else estimate
        lines = max(1, math.ceil(width_px / max(1, area["usable_width_px"])))
        flags = []
        if lines > GUIDE_LINES:
            flags.append("LONG_LINE")
        if cps is not None and cps > READING_CPS:
            flags.append("FAST_READ")
        if 0 < duration < MIN_CUE_MS:
            flags.append("SHORT")
        rows.append({"index": index, "id": cue["id"],
                     "source_row_id": cue["source_row_id"],
                     "repeat_marker_id": cue.get("repeat_marker_id"),
                     "text": text,
                     "start_ms": start, "end_ms": end,
                     "duration_ms": duration,
                     "char_start": cue.get("char_start", 0),
                     "char_end": cue.get("char_end"),
                     "timecode": f"{timecode(start)}–{timecode(end)}",
                     "frames": [frame_at(start, fps), frame_at(end, fps)],
                     "chars": len(text),
                     "chars_per_second": cps,
                     "width_px": width_px,
                     "width_measured": measured is not None,
                     "usable_width_px": area["usable_width_px"],
                     "est_lines": lines,
                     "flags": flags})
    return rows


def _adjacency(cues, fps):
    """Cue pairs that share or nearly share time.

    The stored validator already rejects overlaps; the report still names
    every seam so the reviewer can hear it: OVERLAP (candidates/foreign
    documents only), BACK_TO_BACK (end == next start), TIGHT_GAP (shorter
    than one frame) and GAP.
    """
    frame_ms = 1000 / fps
    pairs = []
    for left, right in zip(cues, cues[1:]):
        gap = right["start_ms"] - left["end_ms"]
        if gap < 0:
            relation = "OVERLAP"
        elif gap == 0:
            relation = "BACK_TO_BACK"
        elif gap < frame_ms:
            relation = "TIGHT_GAP"
        else:
            relation = "GAP"
        pairs.append({"left_id": left["id"], "right_id": right["id"],
                      "gap_ms": gap, "relation": relation,
                      "at_ms": left["end_ms"],
                      "at_timecode": timecode(left["end_ms"])})
    return pairs


def _repeat_report(document):
    """Repeat markers and their expansion state (design: explicit, source-
    bound expansions only — never silent duplication)."""
    rows = {row["id"]: row for row in document.get("rows", [])}
    expansions = document.get("repeat_expansions", {}) or {}
    cues_by_row = {}
    for cue in document.get("cues", []):
        cues_by_row.setdefault(cue["source_row_id"], []).append(cue["id"])
    report = []
    for row in document.get("rows", []):
        if row["kind"] != "repeat":
            continue
        suggested = row.get("suggested_source_row_ids", [])
        chosen = expansions.get(row["id"])
        expanded = [{"row_id": source, "effective_id": f"{row['id']}:{source}",
                     "text": rows.get(source, {}).get("text"),
                     "timed": bool(cues_by_row.get(f"{row['id']}:{source}"))}
                    for source in suggested]
        report.append({"marker_row_id": row["id"], "line": row["line"],
                       "section": row.get("section"),
                       "suggested_source_row_ids": suggested,
                       "expansion": chosen,
                       "expanded_rows": expanded,
                       "resolved": chosen == suggested
                       and all(cues_by_row.get(r["effective_id"])
                               for r in expanded)})
    return report


def _coverage_gaps(document):
    """Per effective row, the non-whitespace source spans no cue covers."""
    effective = document.get("effective_rows")
    if effective is None:
        try:
            effective, _ = _effective_rows(document)
        except FilmError:
            effective = []
    coverage = {row["id"]: [] for row in effective}
    for cue in document.get("cues", []):
        source = cue.get("source_row_id")
        if source in coverage:
            coverage[source].append((cue.get("char_start", 0),
                                     cue.get("char_end",
                                             len(cue.get("text", "")))))
    gaps = []
    for row in effective:
        spans, cursor = [], 0
        for first, last in sorted(coverage[row["id"]]):
            if row["text"][cursor:first].strip():
                spans.append([cursor, first])
            cursor = last
        if row["text"][cursor:].strip():
            spans.append([cursor, len(row["text"])])
        for first, last in spans:
            gaps.append({"row_id": row["id"],
                         "original_source_row_id":
                             row.get("original_source_row_id"),
                         "span": [first, last],
                         "text": row["text"][first:last]})
    return gaps


def _font_analysis(p, config, document):
    """Glyph coverage of the real cue characters (the same cmap check Final
    gates on) plus the cue ids that carry each missing codepoint."""
    text = "".join(cue["text"] for cue in document.get("cues", []))
    try:
        font_file, report = _font(p, config, text)
    except FilmError as exc:
        return {"font_file": None,
                "report": {"status": "error", "family": None,
                           "requested_family": (config.get("subtitles") or {})
                           .get("font_name", "DejaVu Sans"),
                           "missing_codepoints": [], "note": str(exc)},
                "missing_by_cue": {}, "final_blocked": True}
    missing = set()
    for codepoint in report.get("missing_codepoints", []):
        match = _FONT.fullmatch(codepoint)
        if match:
            missing.add(chr(int(match.group(1), 16)))
    missing_by_cue = {}
    if missing:
        for cue in document.get("cues", []):
            bad = sorted(set(cue["text"]) & missing)
            if bad:
                missing_by_cue[cue["id"]] = bad
    return {"font_file": font_file, "report": report,
            "missing_by_cue": missing_by_cue,
            "final_blocked": report["status"] != "verified"}


def _row_texts(rows):
    return [row["text"] for row in rows]


def _diff_rows(before_rows, after_rows, *, before_label, after_label):
    """Row-level text diff — what actually changed in the lyric source."""
    before, after = _row_texts(before_rows), _row_texts(after_rows)
    matcher = difflib.SequenceMatcher(a=before, b=after)
    changes = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append({"op": tag,
                        "before_lines": [r["line"] for r in before_rows[a0:a1]],
                        "after_lines": [r["line"] for r in after_rows[b0:b1]],
                        "before": before[a0:a1], "after": after[b0:b1]})
    return {"from": before_label, "to": after_label,
            "before_rows": len(before), "after_rows": len(after),
            "changed": bool(changes), "changes": changes}


def _latest_history(p):
    folder = Path(p) / "lyrics/history"
    candidates = sorted(folder.glob("timing_*.json"),
                        key=lambda f: f.stat().st_mtime) \
        if folder.is_dir() else []
    for path in reversed(candidates):
        try:
            return path, read(path)
        except (FilmError, ValueError, TypeError):
            continue
    return None, None


def source_diff(project):
    """The lyric-source diff the review cares about.

    Two honest views: (1) `pending` — input/lyrics.txt edited but not yet
    prepared, diffed against the last prepared mirror bytes; (2)
    `invalidated` — the newest archived timing document's source rows
    against the current source rows, i.e. what the previous timing was
    authored on versus what is sung now. An unchanged source reports
    `changed: False`; nothing here writes.
    """
    p = Path(project)
    source_path = safe_path(p, "input/lyrics.txt")
    mirror_path = safe_path(p, "lyrics/lyrics_source.txt")
    result = {"kind": "lyrics_source_diff", "pending": None,
              "invalidated": None, "facets": dict(FACETS)}
    if not source_path.is_file():
        result["missing_source"] = True
        return result
    data, source_hash, rows, missing = _source(p)

    if mirror_path.is_file() and mirror_path.read_bytes() != data:
        old = mirror_path.read_bytes()
        try:
            old_text = old.decode("utf-8-sig")
        except UnicodeDecodeError:
            old_text = ""
        old_rows = [{"line": i, "text": line} for i, line in
                    enumerate(old_text.splitlines(), 1) if line.strip()]
        diff = _diff_rows(old_rows, rows,
                          before_label="lyrics/lyrics_source.txt (준비된 원문)",
                          after_label="input/lyrics.txt (편집된 원문)")
        diff["note"] = ("원문이 바뀌었습니다 — 준비하면 기존 타이밍·검토는 "
                        "이력으로 보관되고 새 타이밍을 다시 검토해야 합니다")
        result["pending"] = diff

    history_path, history_doc = _latest_history(p)
    if history_doc and history_doc.get("source_sha256") != source_hash:
        diff = _diff_rows(history_doc.get("rows", []), rows,
                          before_label=(f"폐기된 타이밍 원문 "
                                        f"({history_path.name})"),
                          after_label="input/lyrics.txt (현재 원문)")
        diff["note"] = ("폐기된 타이밍이 어떤 원문에 묶여 있었는지 "
                        "보여줍니다 — 새 검토는 현재 원문 기준입니다")
        diff["archived_document"] = history_path.name
        result["invalidated"] = diff
    return result


def cue_at(document, position_ms, fps=24):
    """The cue under a listening position — and what flanks it.

    Positions are original-master integer milliseconds (the lyric clock is
    independent of cut edits). The answer names the active cue or the gap
    the position falls in, plus the surrounding cues and the integer
    frame the position maps to.
    """
    if type(position_ms) is not int or position_ms < 0:
        raise FilmError("청취 위치는 0 이상의 정수 밀리초입니다")
    cues = document.get("cues", [])
    previous, active, nxt = None, None, None
    for index, cue in enumerate(cues):
        if cue["start_ms"] <= position_ms < cue["end_ms"]:
            active = index
            break
        if cue["end_ms"] <= position_ms:
            previous = index
        elif cue["start_ms"] > position_ms and nxt is None:
            nxt = index
            break
    if active is not None:
        state = "ON_CUE"
        if active + 1 < len(cues):
            nxt = active + 1
    elif not cues:
        state = "AFTER_LAST"
    elif position_ms < cues[0]["start_ms"]:
        state = "BEFORE_FIRST"
        nxt = 0
    elif previous is None:
        state = "AFTER_LAST"
        previous = len(cues) - 1
    elif nxt is None:
        state = "IN_GAP" if cues[-1]["end_ms"] > position_ms \
            else "AFTER_LAST"
        if state == "AFTER_LAST":
            previous = len(cues) - 1
    else:
        state = "IN_GAP"

    def brief(index):
        cue = cues[index]
        return {"index": index, "id": cue["id"], "text": cue["text"],
                "source_row_id": cue["source_row_id"],
                "start_ms": cue["start_ms"], "end_ms": cue["end_ms"],
                "frames": [frame_at(cue["start_ms"], fps),
                           frame_at(cue["end_ms"], fps)]}
    return {"kind": "cue_position", "position_ms": position_ms,
            "position_timecode": timecode(position_ms),
            "position_frame": frame_at(position_ms, fps),
            "state": state, "cue": brief(active) if active is not None else None,
            "previous": brief(previous) if previous is not None else None,
            "next": brief(nxt) if nxt is not None else None,
            "gap_before_ms": (position_ms - cues[previous]["end_ms"]
                              if previous is not None else None),
            "gap_after_ms": (cues[nxt]["start_ms"] - position_ms
                             if nxt is not None else None),
            "facets": dict(FACETS)}


def evaluate_candidate(project, candidate):
    """Measure an imported/ASR timing candidate — without approving it.

    The candidate is validated against the same rules `save_timing`
    enforces, but nothing is written and any `review` field the candidate
    carries is stripped and named: a candidate can never arrive reviewed.
    Its state is always UNREVIEWED — only a named human reviewer via
    `save_timing` turns a candidate into approved timing.
    """
    p = Path(project)
    result = {"kind": "lyrics_candidate_report", "valid": False,
              "errors": [], "warnings": [], "analysis": None,
              "review_state": "UNREVIEWED",
              "candidate_review_ignored": False,
              "human_review_required": True,
              "note": ("가져온 후보는 사람이 실제 보컬을 듣고 검토하기 전까지 "
                       "승인되지 않습니다 — 후보 안의 review 필드는 무시됩니다"),
              "facets": dict(FACETS)}
    if not isinstance(candidate, dict):
        result["errors"].append("candidate must be a timing document object")
        return result
    data, source_hash, fresh_rows, missing = _source(p)
    if candidate.get("schema_version") != SCHEMA_VERSION:
        result["errors"].append("candidate schema_version must be "
                                + str(SCHEMA_VERSION))
        return result
    if candidate.get("source_sha256") != source_hash:
        result["errors"].append(
            "candidate is bound to a different lyrics source — re-export "
            "the candidate against the current input/lyrics.txt")
        return result
    try:
        checked = json.loads(json.dumps(candidate))
    except (TypeError, ValueError) as exc:
        # A candidate holding values no parsed JSON document can carry
        # (sets, circular refs) gets the documented invalid report — the
        # engine API never raises out for garbage it exists to judge.
        result["errors"].append(f"candidate is not a JSON document: {exc}")
        return result
    if "review" in checked:
        result["candidate_review_ignored"] = True
        checked.pop("review", None)
    checked["rows"] = fresh_rows
    checked["source_path"] = "input/lyrics.txt"
    checked["source_missing"] = missing
    duration = _duration(p)
    if duration is None:
        result["errors"].append("analysis/audio.json has no duration_ms — "
                                "analyze the master before judging timing")
        return result
    # Tolerant seam scan first so an invalid candidate still shows every
    # simultaneous/overlapping cue, then the authoritative _check verdict.
    fps = int(_config(p).get("format", {}).get("fps", 24))
    try:
        result["seams"] = _adjacency(checked.get("cues", []), fps)
    except (FilmError, KeyError, TypeError, ValueError):
        # Cues too malformed for even the tolerant scan — _check below
        # names the defect and returns the documented invalid report.
        pass
    try:
        checked, warnings = _check(checked, duration)
    except (FilmError, KeyError, TypeError, ValueError) as exc:
        result["errors"].append(str(exc))
        return result
    result["valid"] = True
    result["warnings"] = warnings
    result["unresolved_row_ids"] = checked["unresolved_row_ids"]
    if checked["unresolved_row_ids"]:
        result["warnings"].append(
            "unresolved rows remain — a reviewer approval is refused "
            "until every row and repeat is timed")
    area = _safe_area(_config(p))
    font = _font_analysis(p, _config(p), checked)
    result["analysis"] = {
        "cues": _cue_rows(checked, fps, font["font_file"], area),
        "repeats": _repeat_report(checked),
        "coverage_gaps": _coverage_gaps(checked),
        "font": {k: v for k, v in font.items() if k != "font_file"},
    }
    result["timing_sha256"] = timing_fingerprint(checked)
    return result


def _legacy_lock_state(p):
    locks = safe_path(p, "manifest/locks.json")
    record = read(locks, {}) if locks.is_file() else {}
    if not record.get("fingerprint"):
        return {"state": "UNLOCKED", "lock_id": None}
    from .core import production_fingerprint
    try:
        current = production_fingerprint(p)
    except (FilmError, OSError, KeyError, TypeError):
        return {"state": "UNVERIFIABLE", "lock_id": None,
                "mock_only": bool(record.get("mock_only")), "changed": []}
    return {"state": "CURRENT" if record["fingerprint"] == current
            else "STALE",
            "lock_id": None, "mock_only": bool(record.get("mock_only")),
            "changed": ([] if record["fingerprint"] == current
                        else ["fingerprint"])}


def _review_state(document):
    review = document.get("review")
    # The same liveness rule the gates apply (_check/validate_lyrics): a
    # stored review counts only while it carries the review schema version
    # they read and names the human who affirmed it — a record without
    # either is the "never reviewed" every gate enforces, whatever hash it
    # claims.
    if review and review.get("schema_version") == REVIEW_SCHEMA_VERSION \
            and str(review.get("reviewer", "")).strip():
        return {"state": "CURRENT", "reviewer": review.get("reviewer"),
                "reviewed_at": review.get("reviewed_at"),
                "lyrics_review_sha256": review.get("lyrics_review_sha256"),
                "timing_sha256": review.get("timing_sha256")}
    return {"state": "UNREVIEWED"}


def _builds_lyrics(p, current_review_sha):
    """Per sealed build: clean vs subbed as separate artifacts plus the
    lyric binding each one captured — never a single shared hash."""
    from .builds import list_builds
    rows = []
    for build in list_builds(p):
        if build.get("status") != "COMPLETE":
            continue
        folder = Path(build["build_dir"])
        files = build.get("files", {})
        outputs = build.get("outputs") or {}
        clean_sha = (outputs.get("clean") or {}).get("sha256") \
            or files.get("MASTER_CLEAN.mp4")
        subbed_sha = (outputs.get("subbed") or {}).get("sha256") \
            or files.get("MASTER_SUBBED.mp4")
        lyric = build.get("lyrics") or {}
        sealed_review = lyric.get("lyrics_review_sha256") \
            or lyric.get("review_sha256")
        report_path = folder / "subtitle_report.json"
        sealed = read(report_path, {}) if report_path.is_file() else {}
        rows.append({
            "build_id": build["build_id"],
            "mode": build.get("mode"),
            "clean_sha256": clean_sha,
            "subbed_sha256": subbed_sha,
            "distinct_outputs": bool(clean_sha and subbed_sha
                                     and clean_sha != subbed_sha),
            "sequences": {
                "clean_sequence_root":
                    (build.get("sequences") or {})
                    .get("clean_sequence_root"),
                "subbed_sequence_root":
                    (build.get("sequences") or {})
                    .get("subbed_sequence_root")},
            "lyrics": {"timing_sha256": lyric.get("timing_sha256")
                       or sealed.get("timing_sha256"),
                       "lyrics_review_sha256": sealed_review,
                       "cues": lyric.get("cues"),
                       "font_sha256":
                           ((build.get("subtitles") or {})
                            .get("font") or {}).get("sha256")},
            "review_matches_current":
                (sealed_review == current_review_sha)
                if sealed_review and current_review_sha else None,
            "re_review_scope": ("자막·폰트 변경은 이 빌드의 sealed 바이트를 "
                                "바꾸지 않습니다 — 새 전달물은 새 빌드와 "
                                "새 검토가 필요합니다")})
    return rows


def invalidation_report(project, change="TIMING"):
    """Which approvals a lyric-side change invalidates — and which survive.

    `change` is TIMING (cue start/end edits), SUBTITLE_SETTINGS
    (font_size/margins/family), FONT (font bytes) or SOURCE_TEXT (the lyric
    wording itself). Cue/font/settings changes touch only the subbed
    delivery — clean sequence and cut-motion reviews stay bound. A source
    text change additionally rewrites the shared visual intent
    (`input/lyrics.txt` participates in `visual_context_fingerprint` /
    `shared_intent_digest`), so visual approvals go stale as well.
    """
    if change not in CHANGE_KINDS:
        raise FilmError("change must be one of " + ", ".join(CHANGE_KINDS))
    p = Path(project)
    config = _config(p)
    profile = production_profile(config)
    report = {"kind": "lyrics_invalidation", "change": change,
              "profile": profile, "facets": dict(FACETS),
              "lyrics_review": {"after": "UNREVIEWED",
                                "reason": "사람이 다시 들어 확인해야 "
                                          "가사 검수가 복구됩니다"}}

    if profile == "FRAME_ANIMATION_V1":
        from .animation_locks import lock_status
        try:
            locks = lock_status(p)
            report["locks_now"] = {
                "plan": locks["plan"]["state"],
                "waves": {w: s["state"] for w, s in locks["waves"].items()},
                "final": locks["final"]["state"]}
        except FilmError as exc:
            report["locks_now"] = {"unavailable": str(exc)}
        if change == "SOURCE_TEXT":
            # input/lyrics.txt is inside shared_intent_digest, which every
            # CUT review, WAVE_LOCK, PLAN_LOCK and FINAL_LOCK binds.
            report["projection"] = {
                "method": "binding-fields",
                "stale": ["모든 CUT/TRANSITION 검수 (shared intent 변경)",
                          "PLAN_LOCK", "모든 WAVE_LOCK", "FINAL_LOCK",
                          "FINAL_FILM"],
                "kept": [],
                "reason": "가사 원문은 공통 시각 의도에 포함됩니다 — "
                          "컷·전환 검수까지 낡아집니다"}
        else:
            classes = (["CUE"] if change == "TIMING"
                       else ["FONT"] if change == "FONT"
                       else ["CUE", "FONT"])
            try:
                from .render_provenance import plan_rebuild
                plan = plan_rebuild(p, [{"class": c} for c in classes])
                proj = plan["stale_approval_projection"]
                report["projection"] = {
                    "method": "ANIM-020 plan_rebuild",
                    "stale": proj["stale"], "kept": proj["kept"],
                    "cuts": proj["cuts"], "transitions": proj["transitions"],
                    "locks": proj["locks"], "final_film": proj["final_film"],
                    "closure": {"dirty_frames":
                                plan["closure"]["dirty_frames"],
                                "subbed_dirty_frames":
                                plan["closure"]["subbed_dirty_frames"],
                                "subbed_ranges":
                                plan["closure"]["subbed_ranges"],
                                "encode_roles":
                                plan["closure"]["encode_roles"]},
                    "reason": ("cue·폰트 변경은 subbed 전달물만 다시 만듭니다 "
                               "— clean 시퀀스와 컷 동작 검수는 유지됩니다")}
            except FilmError as exc:
                report["projection"] = {"method": "ANIM-020 plan_rebuild",
                                        "unavailable": str(exc),
                                        "stale": ["FINAL_LOCK", "FINAL_FILM",
                                                  "가사 검수"],
                                        "kept": ["CUT/TRANSITION 검수",
                                                 "PLAN_LOCK", "WAVE_LOCK"],
                                        "reason": "cue·폰트 변경은 subbed "
                                                  "전달물만 다시 만듭니다"}
        report["survives"] = ([] if change == "SOURCE_TEXT" else
                              ["CUT 검수", "TRANSITION 검수", "PLAN_LOCK",
                               "WAVE_LOCK", "clean 시퀀스"])
    else:
        # LEGACY_MV: the production fingerprint binds lyrics_timed.json,
        # the subtitles config and font_file bytes directly.
        report["locks_now"] = {"production": _legacy_lock_state(p)["state"]}
        if change == "SOURCE_TEXT":
            stale = ["production LOCK", "가사 검수", "모든 영상(샷) 검수"]
            kept = []
            reason = ("가사 원문은 visual_context_fingerprint에 포함됩니다 "
                      "— 샷 검수도 다시 필요합니다")
        else:
            # TIMING/SUBTITLE_SETTINGS always move the production
            # fingerprint (lyrics_timed.json, subtitles config). FONT does
            # only when a configured font_file path is hashed; a
            # fontconfig-resolved font still stales the lyric review.
            stale = ["가사 검수"]
            if change != "FONT" \
                    or (config.get("subtitles") or {}).get("font_file"):
                stale.insert(0, "production LOCK")
            kept = ["샷별 영상 검수 (타이밍·자막 설정은 시각 검수 범위 밖)"]
            reason = ("타이밍·자막 설정·폰트 변경은 가사 검수를 다시 "
                      "요구합니다 — 샷 영상 검수는 유지됩니다")
        report["projection"] = {"method": "production_fingerprint",
                                "stale": stale, "kept": kept,
                                "reason": reason}
        report["survives"] = kept
    report["clean_vs_subbed"] = (
        "clean 마스터와 subbed 마스터는 별개 해시의 별개 결과물입니다 — "
        "자막 변경은 subbed(전달물) 쪽만 다시 인코딩하고 clean 시각 결과는 "
        "그대로입니다")
    return report


def workbench(project):
    """The lyric/cue review workbench read model.

    One pass over the shared lyric documents: source state and pending /
    invalidated source diffs, per-cue rows with integer-frame boundaries
    and reading metrics, repeat-expansion state, seam/gap pairs, coverage
    gaps, the font cmap verdict and which cues carry missing glyphs, the
    current review state, and the sealed builds' clean-vs-subbed lyric
    binding. Writes nothing except the same sync `prepare_lyrics` already
    performs for every other consumer.
    """
    p = Path(project)
    config = _config(p)
    profile = production_profile(config)
    fmt = config.get("format", {})
    fps = int(fmt.get("fps", 24))
    duration = _duration(p)
    board = {"kind": "lyrics_review_board", "project": str(p),
             "profile": profile, "fps": fps,
             "canvas": {"width": int(fmt.get("width", 1440)),
                        "height": int(fmt.get("height", 1080))},
             "duration_ms": duration,
             "duration_seconds": (duration / 1000) if duration else None,
             "facets": dict(FACETS)}

    diff = source_diff(p)
    board["source_diff"] = diff

    _, source_hash, _, source_missing = _source(p)
    board["source"] = {"path": "input/lyrics.txt", "sha256": source_hash,
                       "missing": source_missing}

    document = prepare_lyrics(p)
    # The stored approval before validate_lyrics strips one it can no longer
    # bind — kept so the board can tell a stale approval from "never
    # reviewed" on the analysis-present path too.
    stored_review = document.get("review")
    warnings = []
    if duration is None:
        warnings.append("analysis/audio.json이 없어 타이밍 범위를 검사할 "
                        "수 없습니다 — 먼저 음원을 분석하세요")
    else:
        document, warnings = validate_lyrics(p, duration, strict=False)
    board["warnings"] = warnings

    effective = document.get("effective_rows")
    if effective is None:
        try:
            effective, _ = _effective_rows(document)
        except FilmError:
            effective = []
    rows = document.get("rows", [])
    board["rows"] = {"total": len(rows),
                     "lyric": sum(1 for r in rows if r["kind"] == "lyric"),
                     "section": sum(1 for r in rows if r["kind"] == "section"),
                     "repeat": sum(1 for r in rows if r["kind"] == "repeat"),
                     "effective": len(effective),
                     "unresolved": document.get("unresolved_row_ids", [])}

    area = _safe_area(config)
    font = _font_analysis(p, config, document)
    board["area"] = area
    board["font"] = {k: v for k, v in font.items() if k != "font_file"}
    board["font"]["font_file"] = config.get("subtitles", {}).get("font_file")

    cue_rows = _cue_rows(document, fps, font["font_file"], area)
    missing_by_cue = font["missing_by_cue"]
    for row in cue_rows:
        if row["id"] in missing_by_cue:
            row["flags"].append("MISSING_GLYPHS")
            row["missing_glyphs"] = missing_by_cue[row["id"]]
    board["cues"] = cue_rows
    board["seams"] = _adjacency(document.get("cues", []), fps)
    board["repeats"] = _repeat_report(document)
    board["coverage_gaps"] = _coverage_gaps(document)

    board["timing_sha256"] = timing_fingerprint(document)
    review = _review_state(document)
    if review["state"] == "UNREVIEWED" and isinstance(stored_review, dict):
        # validate_lyrics nulls an unbindable stored approval before this
        # board sees the document — the honest label for it is the STALE
        # the fingerprint check below assigns, matching the
        # missing-analysis path, never "never reviewed".
        review = _review_state({"review": stored_review})
    try:
        review["current_sha256"] = lyrics_review_fingerprint(p, document)
    except (FilmError, OSError, KeyError, TypeError, ValueError) as exc:
        review["current_sha256"] = None
        review["fingerprint_error"] = str(exc)
    if review["state"] == "CURRENT":
        # The same verdict validate_lyrics reaches: the stored approval
        # counts only while it binds the exact current document/settings/
        # font, and the timing it was affirmed over — a fingerprint it can
        # no longer match is stale, never silently revived.
        if review["current_sha256"] is None:
            review["state"] = "UNVERIFIABLE"
        elif review["lyrics_review_sha256"] != review["current_sha256"] \
                or review["timing_sha256"] != board["timing_sha256"]:
            review["state"] = "STALE"
    board["review"] = review
    board["document_path"] = "lyrics/lyrics_timed.json"

    counts = {"cues": len(cue_rows),
              "long_lines": sum(1 for c in cue_rows if "LONG_LINE" in c["flags"]),
              "fast_reads": sum(1 for c in cue_rows if "FAST_READ" in c["flags"]),
              "missing_glyph_cues": len(missing_by_cue),
              "unresolved_rows": len(board["rows"]["unresolved"]),
              "repeat_markers": len(board["repeats"]),
              "seams": len(board["seams"]),
              "back_to_back": sum(1 for s in board["seams"]
                                  if s["relation"] == "BACK_TO_BACK")}
    board["counts"] = counts
    board["builds"] = _builds_lyrics(p, review.get("current_sha256"))
    return board
