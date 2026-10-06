"""FRAME_ANIMATION_V1 per-track exposure schedules (ANIM-004, design 7).

An ExposureSchedule partitions one track's owned output range [start, end)
into slots; every slot carries a drawing (a member index of the bound frame
sequence) or a state (camera/transform description), its exposure duration,
an intentional-hold marker and an optional transform recipe. The contract this
enforces:

- every output slot is covered — an empty slot is never filled with the
  previous drawing to look complete (BLOCK-EMPTY-EXPOSURE);
- shared anchors belong once, at the next segment's start — a tool that
  returns both endpoints has the duplicate removed explicitly by
  `adopt_segment_output`, and adjacent slots repeating one drawing must carry
  an explicit `continued` declaration;
- a sub-stride trailing exposure (an odd-length twos tail) is recorded as
  `partial: "ODD_END"`, distinct from an accidental anchor duplicate;
- a multi-frame exposure on a strided track must be a declared hold;
- motion never interpolates across a cut boundary — a schedule cannot own
  frames outside its declared range.

Schedules are structures embedded in the shot plan; this module validates,
expands and digests them. It creates no assets or approvals.
"""
import hashlib

from .animation_schema import _check_canon, canon_bytes
from .core import FilmError

SCHEDULE_FIELDS = {"track", "start", "end", "stride", "phase", "slots"}
SLOT_FIELDS = {"start", "end", "drawing", "state", "hold", "transform",
               "continued", "partial"}
PARTIAL_KINDS = {"PHASE", "ODD_END"}


def _int(value, what, minimum=0):
    if type(value) is not int or value < minimum:
        raise FilmError(f"{what} must be an integer >= {minimum}")
    return value


def _slot_exposure(slot, index):
    frames = slot["end"] - slot["start"]
    has_drawing = slot.get("drawing") is not None
    has_state = type(slot.get("state")) is dict
    if has_drawing and has_state:
        raise FilmError(f"Slot {index} carries both a drawing and a state")
    if has_drawing:
        _int(slot["drawing"], f"slot {index} drawing")
    return frames, has_drawing or has_state


def validate_schedule(schedule):
    """Structural contract of one exposure schedule over its owned range."""
    if type(schedule) is not dict or set(schedule.keys()) - SCHEDULE_FIELDS:
        raise FilmError("Malformed exposure schedule")
    missing = SCHEDULE_FIELDS - schedule.keys()
    if missing:
        raise FilmError(f"Exposure schedule missing fields: {sorted(missing)}")
    if type(schedule["track"]) is not str or not schedule["track"]:
        raise FilmError("track must be a non-empty string")
    start = _int(schedule["start"], "schedule start")
    end = _int(schedule["end"], "schedule end", 1)
    if end <= start:
        raise FilmError("A schedule must own at least one output frame")
    stride = _int(schedule["stride"], "stride", 1)
    phase = _int(schedule["phase"], "phase")
    if phase >= stride:
        raise FilmError("phase must stay below stride")
    slots = schedule["slots"]
    if type(slots) is not list or not slots:
        raise FilmError("An exposure schedule needs a non-empty slot list")
    empty, cursor = [], start
    previous_drawing = None
    for index, slot in enumerate(slots):
        if type(slot) is not dict or set(slot.keys()) - SLOT_FIELDS:
            raise FilmError(f"Malformed exposure slot {index}")
        for field in ("start", "end"):
            _int(slot.get(field), f"slot {index} {field}")
        if slot["end"] <= slot["start"]:
            raise FilmError(f"Exposure slot {index} is empty or reversed")
        if slot["start"] != cursor:
            raise FilmError("Exposure slots must cover the owned range without gaps")
        cursor = slot["end"]
        frames, filled = _slot_exposure(slot, index)
        if not filled:
            empty.append(index)
        for flag in ("hold", "continued"):
            if type(slot.get(flag, False)) is not bool:
                raise FilmError(f"slot {index} {flag} must be a boolean")
        partial = slot.get("partial")
        if partial is not None and partial not in PARTIAL_KINDS:
            raise FilmError(f"slot {index} partial must be one of {sorted(PARTIAL_KINDS)}")
        if "transform" in slot:
            _check_canon(slot["transform"])
        if "state" in slot and slot["state"] is not None:
            _check_canon(slot["state"])
        if frames > stride and slot.get("hold") is not True:
            raise FilmError(
                f"Slot {index} exceeds the {stride}-frame stride without a declared hold")
        if frames < stride:
            if partial == "ODD_END":
                if index != len(slots) - 1:
                    raise FilmError("An odd-end exposure is only the last slot")
            elif partial == "PHASE":
                if index != 0 or frames != stride - phase or phase == 0:
                    raise FilmError("A PHASE slot must lead the schedule and match the phase")
            else:
                raise FilmError(
                    f"Slot {index} is shorter than the {stride}-frame stride "
                    "without an ODD_END/PHASE marker")
        drawing = slot.get("drawing")
        if slot.get("continued") is True and drawing is None:
            raise FilmError(f"Slot {index} is marked continued without a drawing")
        if index > 0:
            # A repeated drawing between adjacent slots is a shared anchor:
            # it belongs once to the next segment's start, so the second
            # slot must declare the continuation explicitly.
            if drawing is not None and drawing == previous_drawing \
                    and slot.get("continued") is not True:
                raise FilmError(
                    f"Slot {index} repeats the previous drawing; a shared anchor "
                    "must be declared continued, not duplicated")
            if drawing != previous_drawing and slot.get("continued") is True:
                raise FilmError(
                    f"Slot {index} is marked continued but changes the drawing")
        previous_drawing = drawing
    if cursor != end:
        raise FilmError("Exposure slots must end exactly at the schedule end")
    if empty:
        raise FilmError(
            "Empty exposure slots at "
            + ", ".join(str(i) for i in empty)
            + " — an empty slot is never filled with the previous drawing")
    return {"track": schedule["track"], "range": [start, end],
            "stride": stride, "phase": phase, "slots": len(slots),
            "frames": end - start,
            "distinct_drawings": len({s["drawing"] for s in slots
                                      if s.get("drawing") is not None})}


def expand(schedule):
    """One row per owned output frame, in order, after full validation."""
    validate_schedule(schedule)
    rows = []
    for index, slot in enumerate(schedule["slots"]):
        for frame in range(slot["start"], slot["end"]):
            rows.append({"frame": frame, "slot": index,
                         "drawing": slot.get("drawing"),
                         "state": slot.get("state"),
                         "hold": slot.get("hold", False)
                                 or frame != slot["start"],
                         "continued": slot.get("continued", False),
                         "transform": slot.get("transform")})
    return rows


def validate_track(schedules, length):
    """Tile one track's schedules over [0, length); check boundary anchors."""
    _int(length, "track length", 1)
    if type(schedules) is not list or not schedules:
        raise FilmError("A track needs at least one exposure schedule")
    track = schedules[0].get("track") if type(schedules[0]) is dict else None
    cursor, slots = 0, 0
    previous_tail = None
    for index, schedule in enumerate(schedules):
        info = validate_schedule(schedule)
        if schedule["track"] != track:
            raise FilmError("All schedules on a track must share the track name")
        if schedule["start"] != cursor:
            raise FilmError("Track schedules must cover [0, length) without gaps")
        cursor = schedule["end"]
        slots += info["slots"]
        first, last = schedule["slots"][0], schedule["slots"][-1]
        if index == 0:
            if first.get("continued") is True:
                raise FilmError("The first slot of a track cannot continue a drawing")
        else:
            same_anchor = (first.get("drawing") is not None
                           and first["drawing"] == previous_tail.get("drawing"))
            if same_anchor and first.get("continued") is not True:
                raise FilmError(
                    "A shared anchor is duplicated across a segment boundary; "
                    "the next segment owns it once — mark it continued")
            if first.get("continued") is True and not same_anchor:
                raise FilmError(
                    "A continued first slot must keep the previous segment's drawing")
        previous_tail = last
    if cursor != length:
        raise FilmError("Track schedules must end exactly at the track length")
    return {"track": track, "length": length, "schedules": len(schedules),
            "slots": slots}


def expand_track(schedules, length):
    """Per-frame rows for a whole track (frames are track-local indices)."""
    validate_track(schedules, length)
    rows = []
    for schedule in schedules:
        rows += expand(schedule)
    return rows


def _slot(start, end, drawing=None, state=None, **flags):
    slot = {"start": start, "end": end, "drawing": drawing, "state": state}
    slot.update(flags)
    return slot


def make_schedule(track, start, end, stride, slots, phase=0):
    """Assemble a schedule; validation happens in validate/expand."""
    return {"track": track, "start": start, "end": end, "stride": stride,
            "phase": phase, "slots": slots}


def ones(track, start, end, items):
    """ONES exposure: one drawing/state per output frame."""
    _int(start, "start")
    _int(end, "end", 1)
    if type(items) is not list or len(items) != end - start:
        raise FilmError("ones() needs exactly one drawing or state per frame")
    slots = []
    for offset, item in enumerate(items):
        if type(item) is dict:
            slots.append(_slot(start + offset, start + offset + 1, state=item))
        else:
            slots.append(_slot(start + offset, start + offset + 1, drawing=item))
    return make_schedule(track, start, end, 1, slots)


def twos(track, start, end, drawings=None, phase=0, odd_end=False):
    """TWOS exposure: each drawing holds for two output frames.

    `phase` 1 starts the schedule on the second half of a pair (a PHASE slot
    of one frame). A range whose length does not divide evenly ends in a
    one-frame exposure only when `odd_end` declares it. `drawings` defaults to
    the slot's own start index — drawing 94 of a 96-frame cut is exposed on
    output frames 94 and 95.
    """
    _int(start, "start")
    _int(end, "end", 1)
    if phase not in (0, 1):
        raise FilmError("twos phase is 0 or 1")
    length = end - start
    if length < 1:
        raise FilmError("A twos schedule must own at least one frame")
    slots, cursor = [], start
    if phase == 1:
        slots.append(_slot(cursor, cursor + 1, partial="PHASE"))
        cursor += 1
    while end - cursor >= 2:
        slots.append(_slot(cursor, cursor + 2))
        cursor += 2
    if cursor < end:
        if not odd_end:
            raise FilmError(
                "An odd-length twos range needs an explicit odd_end tail; "
                "one trailing frame is not silently paired")
        slots.append(_slot(cursor, end, partial="ODD_END"))
    if drawings is not None:
        if type(drawings) is not list or len(drawings) != len(slots):
            raise FilmError(
                f"twos() needs one drawing per slot ({len(slots)} slots)")
        for slot, drawing in zip(slots, drawings):
            slot["drawing"] = drawing
    else:
        for slot in slots:
            slot["drawing"] = slot["start"]
    return make_schedule(track, start, end, 2, slots, phase=phase)


def hold(track, start, end, drawing=None, state=None):
    """One declared hold slot spanning the whole range."""
    return make_schedule(track, start, end, 1,
                         [_slot(start, end, drawing=drawing, state=state,
                               hold=True)])


def adopt_segment_output(returned, owned_start, owned_end):
    """Normalize a tool's frames for an owned [start, end) segment.

    A tool returning both endpoints produces `owned + 1` frames; the shared
    anchor is owned once by the next segment's start, so the trailing copy is
    dropped explicitly and reported. Fewer frames than owned, or more than one
    extra frame, are rejected — never padded or speed-changed.
    """
    owned = _int(owned_end, "owned_end", 1) - _int(owned_start, "owned_start")
    if owned < 1:
        raise FilmError("An owned segment must span at least one frame")
    if type(returned) is not list:
        raise FilmError("Segment output must be a list of frames/drawings")
    count = len(returned)
    if count < owned:
        raise FilmError(
            f"Returned {count} frames for an owned range of {owned}; "
            "short output is rejected, not stretched")
    if count > owned + 1:
        raise FilmError(
            f"Returned {count} frames for an owned range of {owned}; "
            "only one shared end anchor may be deduplicated")
    dropped = returned[owned] if count == owned + 1 else None
    return {"drawings": list(returned[:owned]), "dropped_anchor": dropped}


def schedule_digest(schedule):
    """Content digest of a validated schedule; state markers are content."""
    validate_schedule(schedule)
    return hashlib.sha256(canon_bytes(schedule)).hexdigest()
