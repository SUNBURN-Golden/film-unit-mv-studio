"""FRAME_ANIMATION_V1 integer frame clock (ANIM-004, design 6.1).

Internal time is 0-based integer frames over half-open [start, end) ranges.
Display numbers are 1-based (`frame_index + 1`) and stored frames are named
`F_` + six digits. At fps 24 frame `f` starts its exposure at `f/24`s and ends
it at `(f+1)/24`s; ones/twos and source FPS never change the output FPS.
Ratios are kept exact with `fractions.Fraction` and serialized as the
canonical `{"num": n, "den": d}` form (integers, `d > 0`, reduced) that
CANON_JSON_V1 requires.
"""
from fractions import Fraction
from math import gcd
import re

from .core import FilmError

FRAME_FILE = re.compile(r"F_([0-9]{6})\.png")


def check_fps(fps):
    """Output FPS is a positive integer rate (24 for this program's profile)."""
    if type(fps) is not int or fps < 1:
        raise FilmError("fps must be a positive integer")
    return fps


def canon_rational(num, den):
    """Canonical `{"num","den"}` ratio: integers, `den > 0`, gcd 1."""
    if type(num) is not int or type(den) is not int or den <= 0:
        raise FilmError("Rational values need integer num and a positive den")
    g = gcd(abs(num), den)
    return {"num": num // g, "den": den // g}


def fps_rational(fps):
    """The output rate as a canonical rational, e.g. {"num": 24, "den": 1}."""
    return canon_rational(check_fps(fps), 1)


def frame_duration(fps):
    """One frame's exposure duration in seconds, exactly."""
    return Fraction(1, check_fps(fps))


def pts_start(frame_index, fps):
    """Exact second at which `frame_index` starts being displayed."""
    if type(frame_index) is not int or frame_index < 0:
        raise FilmError("frame_index must be a non-negative integer")
    return Fraction(frame_index, check_fps(fps))


def pts_end(frame_index, fps):
    """Exact second at which `frame_index`'s exposure ends."""
    return pts_start(frame_index, fps) + frame_duration(fps)


def pts_rational(frame_index, fps):
    """Canonical rational PTS of `frame_index`'s exposure start."""
    value = pts_start(frame_index, fps)
    return {"num": value.numerator, "den": value.denominator}


def exposure_window(frame_index, fps):
    """(start, end) canonical rationals bounding one frame's exposure."""
    start = pts_start(frame_index, fps)
    end = start + frame_duration(fps)
    return {"start": {"num": start.numerator, "den": start.denominator},
            "end": {"num": end.numerator, "den": end.denominator}}


def display_number(frame_index):
    """1-based management number shown to reviewers and written to files."""
    if type(frame_index) is not int or frame_index < 0:
        raise FilmError("frame_index must be a non-negative integer")
    return frame_index + 1


def frame_filename(frame_index):
    """Stored frame name: F_000001.png is internal frame 0."""
    return f"F_{display_number(frame_index):06d}.png"


def frame_index_of(filename):
    """Inverse of `frame_filename`; anything else is rejected."""
    match = FRAME_FILE.fullmatch(filename if type(filename) is str else "")
    if not match:
        raise FilmError(f"Not an F_000001-style frame name: {filename}")
    return int(match.group(1)) - 1


def frames_for_duration(seconds, fps):
    """Exact frame count for a rational/integer second count.

    The output timeline is integer frames; a duration that does not land on a
    frame boundary is an error, never silently rounded.
    """
    frames = Fraction(seconds) * check_fps(fps)
    if frames.denominator != 1 or frames < 0:
        raise FilmError("Duration does not land on an output frame boundary")
    return int(frames)


def last_exposure_end(frame_count, fps):
    """Exact second at which the last of `frame_count` frames ends."""
    if type(frame_count) is not int or frame_count < 1:
        raise FilmError("frame_count must be a positive integer")
    return Fraction(frame_count, check_fps(fps))
