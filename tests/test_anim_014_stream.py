"""ANIM-014 FrameStream 1: contract, buffer ownership, hand-off checks,
bounded-queue backpressure (schema §12, exec/storage §4.2/§5.2)."""
import threading
import time

import pytest

from anim_014_kit import CONTRACT, HEIGHT, WIDTH, RECIPE, SNAPSHOT

from engine.core import FilmError
from engine.fav_pack import sha256_bytes
from engine.frame_stream import (FrameStream, check_frame,
                                 check_png_handoff, check_rational,
                                 frame_descriptor, fps_fraction,
                                 make_contract)

from anim_013_kit import make_png


def frame(stream, index, data, consumers=("encoder",), producer="renderer"):
    return stream.make_frame(index, data, source_digest=SNAPSHOT,
                             recipe_digest=RECIPE, producer=producer,
                             consumers=consumers)


def pixels(seed=0):
    return bytes((i + seed) % 256 for i in range(WIDTH * HEIGHT * 4))


# -- contract ------------------------------------------------------------

def test_contract_defaults_stride_and_validates():
    contract = make_contract(4, 2, frame_range=[0, 3])
    assert contract["stride"] == 16
    assert contract["pixel_format"] == "RGBA8"
    assert contract["fps"] == {"num": 24, "den": 1}


@pytest.mark.parametrize("patch", [
    {"pixel_format": "YUV420"},
    {"stride": 8},                       # narrower than one pixel row
    {"color_space": "BT709"},
    {"transfer": "PQ"},
    {"color_range": "UNKNOWN"},
    {"alpha_policy": "IGNORE"},
    {"fps": {"num": 24, "den": 2}},      # not reduced
    {"frame_range": [5, 5]},             # empty range
])
def test_contract_rejects_bad_fields(patch):
    base = dict(CONTRACT)
    base.update(patch)
    with pytest.raises(FilmError):
        make_contract(WIDTH, HEIGHT,
                      pixel_format=base["pixel_format"],
                      stride=base["stride"],
                      color_space=base["color_space"],
                      transfer=base["transfer"],
                      color_range=base["color_range"],
                      alpha_policy=base["alpha_policy"],
                      fps=base["fps"], frame_range=base["frame_range"])


def test_rgb8_cannot_carry_alpha_policy():
    with pytest.raises(FilmError):
        make_contract(WIDTH, HEIGHT, pixel_format="RGB8",
                      alpha_policy="STRAIGHT", frame_range=[0, 4])
    make_contract(WIDTH, HEIGHT, pixel_format="RGB8",
                  alpha_policy="OPAQUE", frame_range=[0, 4])


def test_rational_must_be_canonical():
    with pytest.raises(FilmError):
        check_rational({"num": 2, "den": 4}, "fps")     # gcd != 1
    with pytest.raises(FilmError):
        check_rational({"num": 1, "den": -24}, "fps")
    with pytest.raises(FilmError):
        check_rational({"num": 1.5, "den": 1}, "fps")


# -- frames and buffer ownership ------------------------------------------

def test_frame_pts_and_duration_are_rational_clock_truth():
    stream = FrameStream(CONTRACT, queue_depth=2)
    f = frame(stream, 3, pixels())
    assert f.pts.numerator == 1 and f.pts.denominator == 8     # 3/24
    assert f.duration.numerator == 1 and f.duration.denominator == 24
    d = frame_descriptor(f)
    assert d["pts"] == {"num": 1, "den": 8}
    assert d["frame_index"] == 3
    assert d["pixel_sha256"] == sha256_bytes(f.buffer.data)


def test_frame_outside_range_and_bad_buffer_rejected():
    stream = FrameStream(CONTRACT, queue_depth=2)
    with pytest.raises(FilmError, match="outside"):
        frame(stream, 99, pixels())
    with pytest.raises(FilmError, match="stride"):
        frame(stream, 0, b"too-short")
    with pytest.raises(FilmError, match="SHA-256"):
        stream.make_frame(0, pixels(), source_digest="nothex",
                          recipe_digest=RECIPE, producer="p",
                          consumers=("c",))


def test_buffer_cannot_release_before_consumer_acknowledges():
    stream = FrameStream(CONTRACT, queue_depth=2)
    f = frame(stream, 0, pixels(), consumers=("encoder", "muxer"))
    stream.put(f)
    got = stream.get(timeout=0.5)
    assert got is f
    with pytest.raises(FilmError, match="acknowledged"):
        got.buffer.release()
    got.buffer.acknowledge("encoder")
    with pytest.raises(FilmError, match="acknowledged"):
        got.buffer.release()
    got.buffer.acknowledge("muxer")
    got.buffer.release()
    assert got.buffer.released


def test_unknown_consumer_cannot_acknowledge():
    stream = FrameStream(CONTRACT, queue_depth=2)
    f = frame(stream, 0, pixels())
    with pytest.raises(FilmError, match="declared"):
        f.buffer.acknowledge("intruder")


def test_handoff_check_runs_on_put_and_get():
    stream = FrameStream(CONTRACT, queue_depth=2)
    f = frame(stream, 1, pixels())
    assert check_frame(f, CONTRACT)
    stream.put(f)
    got = stream.get(timeout=0.5)
    assert got.frame_index == 1


def test_png_handoff_requires_contract_geometry():
    ok = make_png(seed=1, width=WIDTH, height=HEIGHT)
    assert check_png_handoff(ok, CONTRACT)["width"] == WIDTH
    wrong = make_png(seed=2, width=WIDTH + 1, height=HEIGHT)
    with pytest.raises(FilmError, match="dimensions"):
        check_png_handoff(wrong, CONTRACT)
    with pytest.raises(FilmError, match="PNG"):
        check_png_handoff(b"not-a-png", CONTRACT)


# -- bounded queue / backpressure ------------------------------------------

def test_producer_blocks_when_consumer_lags():
    stream = FrameStream(CONTRACT, queue_depth=2)
    produced = []

    def produce():
        for i in range(5):
            stream.put(frame(stream, i, pixels(seed=i)), timeout=5)
            produced.append(i)
        stream.close()

    t = threading.Thread(target=produce)
    t.start()
    time.sleep(0.2)
    # capacity 2: the producer cannot have queued more than that.
    assert len(produced) <= 2
    got = stream.get(timeout=1)
    got.buffer.acknowledge("encoder")
    time.sleep(0.1)
    assert len(produced) <= 3
    while True:
        got = stream.get(timeout=1)
        if got is None:
            break
        got.buffer.acknowledge("encoder")
    t.join(5)
    assert produced == [0, 1, 2, 3, 4]
    assert stream.stats()["consumed"] == 5


def test_put_times_out_under_sustained_backpressure():
    stream = FrameStream(CONTRACT, queue_depth=1)
    stream.put(frame(stream, 0, pixels()))
    with pytest.raises(FilmError, match="BACKPRESSURE"):
        stream.put(frame(stream, 1, pixels()), timeout=0.1)


def test_closed_stream_refuses_put_but_drains():
    stream = FrameStream(CONTRACT, queue_depth=2)
    stream.put(frame(stream, 0, pixels()))
    stream.close()
    with pytest.raises(FilmError, match="closed"):
        stream.put(frame(stream, 1, pixels()))
    assert stream.get(timeout=0.5).frame_index == 0
    assert stream.get(timeout=0.5) is None
