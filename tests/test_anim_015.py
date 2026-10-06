"""ANIM-015: encode/mux/verify separation and multiple encoder drivers.

Synthetic fixtures only (Pillow frames, a generated sine master). Fake
drivers are labelled FAKE and UNQUALIFIED — they exercise the protocol,
they never qualify a driver, and they can never set NO_FFMPEG_ENCODING.
Hardware absent on this host is reported UNAVAILABLE/hardware-unverified.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from engine import cli
from engine.animation_compiler import encode_build_delivery
from engine.animation_schema import (canon_bytes, check_document,
                                     read_canon, validate_capability_evidence,
                                     validate_encode_recipe, write_canon)
from engine.builds import verify_build
from engine.core import FilmError, digest, ffmpeg, probe, run
from engine.encoder_backends import (DRIVER_NAMES, EncoderDriver,
                                     FrameSource, ManualExportRequired,
                                     capability_evidence, encode_delivery,
                                     encode_digest, get_driver,
                                     make_encode_recipe, probe_all,
                                     probe_driver)
from engine.encoder_backends.service import QualifiedServiceDriver
from engine.media_mux import mux_video_audio, prepare_audio_track
from engine.media_verify import (DELIVERY_PROFILE_MV_H264_AAC_V1,
                                 VERIFY_CONTRACT, check_audio_samples,
                                 check_audio_track, check_video_packets,
                                 check_video_pts, sequence_root,
                                 verify_delivery)
from test_anim_003 import animation_project, make_sequence
from test_anim_006 import approve_all
from test_compiler_v03 import newest_build

SIZE = (64, 48)
FPS = 24


# --- fixtures -------------------------------------------------------------

def make_delivery_frames(folder, count=10, size=SIZE, seed=0):
    """The F_000001.png-style contiguous delivery sequence."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        px = np.zeros((size[1], size[0], 3), np.uint8)
        px[..., 0] = (np.arange(size[0])[None, :] + i * 9 + seed) % 256
        px[..., 1] = (np.arange(size[1])[:, None] + i * 4 + seed) % 256
        px[..., 2] = (i * 13 + seed) % 256
        Image.fromarray(px).save(folder / f"F_{i + 1:06d}.png")
    return folder


def make_master(path, seconds, tone=220):
    import soundfile
    rate = 16000
    t = np.linspace(0, seconds, int(rate * seconds), endpoint=False)
    soundfile.write(str(path), (np.sin(2 * np.pi * tone * t) * 0.2)
                    .astype(np.float32), rate)
    return path


def fixture(tmp_path, count=10):
    """A FrameSource + master + recipe for a full pipeline pass."""
    frames_dir = make_delivery_frames(tmp_path / "frames", count)
    fmt = {"fps": FPS, "width": SIZE[0], "height": SIZE[1], "crf": 18}
    master = make_master(tmp_path / "master.wav", count / FPS)
    frames = FrameSource(frames_dir, FPS, *SIZE)
    recipe = make_encode_recipe("FFMPEG", fmt)
    return frames, recipe, master, fmt


def video_only_mp4(target, frames_dir, count, fps=FPS, size=None,
                   color_args=(), extra=()):
    """Craft an arbitrary video-only MP4 with ffmpeg — used to build
    verifier rejection fixtures; it is not a qualified encode."""
    command = ["-xerror", "-framerate", str(fps), "-start_number", "1",
               "-i", str(Path(frames_dir) / "F_%06d.png"), "-map", "0:v:0",
               "-an", "-r", str(fps), "-c:v", "libx264", "-preset",
               "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
    if size:
        command += ["-s", f"{size[0]}x{size[1]}"]
    command += list(color_args) + list(extra) + [str(target)]
    ffmpeg(command)
    return target


class FakeDriver(EncoderDriver):
    """FAKE test double: produces real packets through a subprocess encode
    so the protocol is exercised end-to-end. evidence_class FAKE means the
    result is always reported UNQUALIFIED and can never set
    NO_FFMPEG_ENCODING."""

    name = "GSTREAMER"
    evidence_class = "FAKE"

    def probe(self, scope=None):
        return {"environment": {"fake": True},
                "registry_state": "DOCUMENTED_ONLY",
                "qualification": "FAKE — test double",
                "reason": "fake driver for protocol tests only"}

    def codec_contract(self, fmt):
        return "fake-subprocess", {"mode": "crf", "value": int(fmt["crf"])}

    def encode(self, frames, recipe, work_dir):
        # A reordering-free elementary stream so the muxer stamps PTS.
        out = Path(work_dir) / "fake_packets.h264"
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{frames.width}x{frames.height}",
             "-framerate", str(frames.fps), "-i", "-",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-x264-params", "bframes=0",
             "-preset", "veryfast", "-crf", "18", "-f", "h264", str(out)],
            input=b"".join(frames.rgb_bytes(i) for i in range(frames.count)),
            capture_output=True)
        if proc.returncode:
            raise FilmError(proc.stderr.decode()[-400:])
        return {"path": out, "timed": False, "reordering_free": True,
                "container": "es", "packets": "h264"}


# --- schema / contract ----------------------------------------------------

def test_encode_recipe_schema_registered_and_exact_fields(tmp_path):
    _, recipe, _, fmt = fixture(tmp_path)
    assert check_document(recipe, "encode_recipe") == "encode_recipe"
    validate_encode_recipe(recipe)
    # Exactly the ADR section-14 fields: driver, codec engine, container,
    # pixel format, timebase, rate control, color, mux.
    assert set(recipe.keys()) == {
        "document_type", "schema_version", "driver", "codec_engine",
        "container", "pixel_format", "timebase", "rate_control", "color",
        "mux"}
    assert recipe["schema_version"] == 1
    assert recipe["driver"] == "FFMPEG"
    assert recipe["container"] == "mp4"
    assert recipe["pixel_format"] == "yuv420p"
    assert recipe["timebase"] == {"num": 1, "den": FPS}
    # CANON_JSON_V1 round trip.
    path = tmp_path / "encode_recipe.json"
    write_canon(path, recipe)
    assert read_canon(path) == recipe


def test_encode_recipe_rejects_bad_driver_and_extra_fields():
    with pytest.raises(FilmError):
        make_encode_recipe("LIBX264", {"fps": 24, "width": 64,
                                       "height": 48, "crf": 18})
    recipe = make_encode_recipe("FFMPEG", {"fps": 24, "width": 64,
                                          "height": 48, "crf": 18})
    bad = {**recipe, "delivery_profile": "sneaky"}
    with pytest.raises(FilmError):
        validate_encode_recipe(bad)
    bad = {**recipe, "schema_version": 2}
    with pytest.raises(FilmError):
        check_document(bad, "encode_recipe")


def test_capability_evidence_schema_registered():
    evidence = probe_driver("QUALIFIED_SERVICE")
    validate_capability_evidence(evidence)
    assert evidence["document_type"] == "capability_evidence"
    assert evidence["schema_version"] == 1
    assert evidence["registry_state"] in (
        "DOCUMENTED_ONLY", "QUALIFIED_FOR_SCOPE", "STALE", "UNAVAILABLE")


def test_encode_digest_covers_contract_not_mp4(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    out = tmp_path / "a.mp4"
    result = encode_delivery(frames, recipe, out, master=master)
    # encode_digest covers sequence root + DeliveryProfile + encoder/mux/
    # verify contract — it is stable across runs and is not the MP4 bytes.
    expected = encode_digest(result["sequence_root"], recipe)
    assert result["encode_digest"] == expected
    assert result["encode_digest"] != digest(out)
    other_root = sequence_root("subbed",
                               list(reversed(frames.pixel_digests())))
    assert encode_digest(other_root, recipe) != result["encode_digest"]
    # A different encoder/mux/verify contract changes the digest.
    other_recipe = dict(recipe, rate_control={"mode": "crf", "value": 23})
    assert encode_digest(result["sequence_root"], other_recipe) \
        != result["encode_digest"]


def test_framesource_contract(tmp_path):
    frames_dir = make_delivery_frames(tmp_path / "frames", 4)
    frames = FrameSource(frames_dir, FPS, *SIZE)
    rows = list(frames)
    assert [r["frame_index"] for r in rows] == [0, 1, 2, 3]
    # Rational PTS: frame i starts at i/24, duration exactly 1/24.
    assert rows[2]["pts"] == __import__("fractions").Fraction(2, 24)
    assert rows[2]["duration"] == __import__("fractions").Fraction(1, 24)
    with pytest.raises(FilmError, match="contiguous"):
        gap = tmp_path / "gap"
        make_delivery_frames(gap, 4)
        (gap / "F_000002.png").unlink()
        FrameSource(gap, FPS, *SIZE)
    with pytest.raises(FilmError, match="expected"):
        FrameSource(frames_dir, FPS, *SIZE, expected_count=5)
    # A frame that does not carry the contracted canvas is rejected.
    odd = tmp_path / "odd"
    make_delivery_frames(odd, 2)
    make_delivery_frames(tmp_path / "odd2", 1, size=(32, 24))
    shutil.copyfile(tmp_path / "odd2/F_000001.png", odd / "F_000002.png")
    source = FrameSource(odd, FPS, *SIZE)
    with pytest.raises(FilmError, match="silently rescale"):
        source.rgb_bytes(1)


# --- real FFMPEG pipeline -------------------------------------------------

def test_ffmpeg_end_to_end_encode_mux_verify(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    out = tmp_path / "delivery.mp4"
    result = encode_delivery(frames, recipe, out, master=master)
    assert result["status"] == "COMPLETE"
    assert result["driver"] == "FFMPEG"
    assert result["no_ffmpeg_encoding"] == "NOT_DEMONSTRATED"
    info = probe(out)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert video["avg_frame_rate"] == "24/1"
    assert int(video["nb_frames"]) == frames.count
    assert audio["codec_name"] == "aac"
    assert result["verification"]["valid"] is True
    assert result["verification"]["pixels"]["frames_compared"] \
        == frames.count


def test_packet_pts_are_exact_rationals(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    out = tmp_path / "delivery.mp4"
    encode_delivery(frames, recipe, out, master=master)
    raw = json.loads(run(["ffprobe", "-v", "error", "-select_streams",
                          "v:0", "-show_entries",
                          "packet=pts,duration,pts_time", "-of", "json",
                          str(out)]))
    packets = raw["packets"]
    assert len(packets) == frames.count
    # First PTS exactly 0, every step exactly 1/24 of the time base.
    assert sorted(int(p["pts"]) for p in packets)[0] == 0
    pts_times = sorted(float(p["pts_time"]) for p in packets)
    assert abs(pts_times[1] - 1 / 24) < 1e-6


def test_verified_audio_track_is_stream_copied_not_reencoded(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    work = tmp_path / "work"
    track = prepare_audio_track(master, frames.count / FPS, work)
    encoded = get_driver("FFMPEG").encode(frames, recipe, work)
    mux_video_audio(encoded, track, tmp_path / "one.mp4", FPS,
                    frames.seconds)
    mux_video_audio(encoded, track, tmp_path / "two.mp4", FPS,
                    frames.seconds)
    # One prepared track, reused twice; the muxer never re-encodes it.
    assert track["path"].read_bytes() == \
        (work / "audio_track.m4a").read_bytes()
    report = check_audio_samples(tmp_path / "one.mp4", frames.seconds,
                                 reference=track["path"])
    assert report["matches_reference"] is True


# --- probes and evidence ---------------------------------------------------

def test_ffmpeg_probe_qualifies_with_real_fixture():
    evidence = probe_driver("FFMPEG")
    validate_capability_evidence(evidence)
    assert evidence["registry_state"] == "QUALIFIED_FOR_SCOPE"
    assert evidence["qualification"] == "real-fixture"
    assert evidence["fixture"]["output_sha256"]
    assert evidence["scope"]["fps"] == {"num": 24, "den": 1}


def test_absent_hardware_reports_unavailable():
    if shutil.which("gst-launch-1.0"):
        pytest.skip("gst-launch-1.0 exists on this host")
    evidence = probe_driver("GSTREAMER")
    validate_capability_evidence(evidence)
    assert evidence["registry_state"] == "UNAVAILABLE"
    assert evidence["qualification"] in ("missing-tool", "missing-plugin")


def test_nvidia_native_never_infers_from_cuda_or_ffmpeg():
    evidence = probe_driver("NVIDIA_NATIVE")
    validate_capability_evidence(evidence)
    try:
        import PyNvVideoCodec  # noqa
        has_sdk = True
    except ImportError:
        has_sdk = False
    if not has_sdk:
        assert evidence["registry_state"] == "UNAVAILABLE"
        assert evidence["qualification"] == "hardware-unverified"


def test_videotoolbox_never_infers_from_platform_name():
    evidence = probe_driver("VIDEOTOOLBOX_NATIVE")
    validate_capability_evidence(evidence)
    import platform
    if platform.system() != "Darwin":
        assert evidence["registry_state"] == "UNAVAILABLE"
        assert evidence["qualification"] == "hardware-unverified"


def test_service_is_documented_only_protocol():
    evidence = probe_driver("QUALIFIED_SERVICE")
    assert evidence["registry_state"] == "DOCUMENTED_ONLY"
    assert evidence["qualification"] == "protocol-only"


def test_unavailable_drivers_refuse_to_encode(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    for name in ("NVIDIA_NATIVE", "VIDEOTOOLBOX_NATIVE"):
        driver = get_driver(name)
        if driver.probe()["registry_state"] == "UNAVAILABLE":
            recipe = make_encode_recipe(name, fmt)
            with pytest.raises(FilmError, match="UNAVAILABLE"):
                encode_delivery(frames, recipe, tmp_path / "x.mp4",
                                driver=driver, master=master)


def test_probe_all_returns_every_driver():
    evidence = probe_all()
    assert set(evidence.keys()) == set(DRIVER_NAMES)
    for record in evidence.values():
        validate_capability_evidence(record)


# --- fake-backed protocol ---------------------------------------------------

def test_fake_driver_protocol_end_to_end(tmp_path):
    """A FAKE driver that produces real packets still passes the full
    mux+verify path — but it is labelled FAKE/UNQUALIFIED and never sets
    NO_FFMPEG_ENCODING."""
    frames, recipe, master, fmt = fixture(tmp_path)
    recipe = make_encode_recipe("GSTREAMER", fmt)
    result = encode_delivery(frames, recipe, tmp_path / "fake.mp4",
                             driver=FakeDriver(), master=master,
                             gate_probe=False)
    assert result["status"] == "COMPLETE"
    assert result["evidence_class"] == "FAKE"
    assert result["qualification_state"] == "UNQUALIFIED"
    assert result["no_ffmpeg_encoding"] == "NOT_DEMONSTRATED"
    info = probe(tmp_path / "fake.mp4")
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["avg_frame_rate"] == "24/1"


class DroppingFake(FakeDriver):
    """Fake that drops the last frame — the verifier must reject."""

    def encode(self, frames, recipe, work_dir):
        out = Path(work_dir) / "drop.mp4"
        # N-1 frames into a timed mp4.
        cmd = ["-xerror", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{frames.width}x{frames.height}",
               "-framerate", str(frames.fps), "-i", "-",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
               "-pix_fmt", "yuv420p", "-frames:v", str(frames.count - 1),
               str(out)]
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-y", *cmd],
            input=b"".join(frames.rgb_bytes(i)
                           for i in range(frames.count - 1)),
            capture_output=True)
        if proc.returncode:
            raise FilmError("dropper failed")
        return {"path": out, "timed": True, "container": "mp4",
                "packets": "h264"}


def test_fake_driver_dropping_frame_is_rejected(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    recipe = make_encode_recipe("GSTREAMER", fmt)
    with pytest.raises(FilmError):
        encode_delivery(frames, recipe, tmp_path / "drop.mp4",
                        driver=DroppingFake(), master=master,
                        gate_probe=False)


# --- verifier rejection paths ----------------------------------------------

def test_verifier_rejects_dropped_frame(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    craft = tmp_path / "dropped.mp4"
    video_only_mp4(craft, frames.dir, frames.count,
                   extra=["-frames:v", str(frames.count - 1)])
    muxed = tmp_path / "dropped_mux.mp4"
    track = prepare_audio_track(master, frames.seconds, tmp_path / "w1")
    mux_video_audio({"path": craft, "timed": True}, track, muxed, FPS,
                    frames.seconds)
    with pytest.raises(FilmError):
        verify_delivery(muxed, frames, recipe, frames.seconds,
                        audio_track=track["path"])


def test_verifier_rejects_duplicated_frame(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    # Same count, but frame 2 is a copy of frame 1 — pixel compare catches.
    dup = tmp_path / "dup"
    make_delivery_frames(dup, frames.count)
    shutil.copyfile(dup / "F_000001.png", dup / "F_000002.png")
    craft = tmp_path / "dup.mp4"
    video_only_mp4(craft, dup, frames.count)
    track = prepare_audio_track(master, frames.seconds, tmp_path / "w2")
    muxed = tmp_path / "dup_mux.mp4"
    mux_video_audio({"path": craft, "timed": True}, track, muxed, FPS,
                    frames.seconds)
    with pytest.raises(FilmError):
        verify_delivery(muxed, frames, recipe, frames.seconds,
                        audio_track=track["path"])


def test_verifier_rejects_wrong_fps(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    craft = tmp_path / "fps.mp4"
    video_only_mp4(craft, frames.dir, frames.count, fps=12)
    track = prepare_audio_track(master, frames.seconds, tmp_path / "w3")
    muxed = tmp_path / "fps_mux.mp4"
    # Mux keeps the wrong-rate video; the verifier rejects it.
    mux_video_audio({"path": craft, "timed": True}, track, muxed, FPS,
                    frames.seconds)
    with pytest.raises(FilmError, match="frame rates|r"):
        verify_delivery(muxed, frames, recipe, frames.seconds,
                        audio_track=track["path"])


def test_verifier_rejects_wrong_size(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    craft = tmp_path / "size.mp4"
    video_only_mp4(craft, frames.dir, frames.count, size=(32, 24))
    track = prepare_audio_track(master, frames.seconds, tmp_path / "w4")
    muxed = tmp_path / "size_mux.mp4"
    mux_video_audio({"path": craft, "timed": True}, track, muxed, FPS,
                    frames.seconds)
    with pytest.raises(FilmError, match="size|contracted"):
        verify_delivery(muxed, frames, recipe, frames.seconds,
                        audio_track=track["path"])


def test_verifier_rejects_missing_audio(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    craft = tmp_path / "silent.mp4"
    video_only_mp4(craft, frames.dir, frames.count)
    track = prepare_audio_track(master, frames.seconds, tmp_path / "w5")
    # Mux video only — simulating a missing audio track.
    ffmpeg(["-xerror", "-i", craft, "-map", "0:v:0", "-c:v", "copy",
            tmp_path / "silent_final.mp4"])
    with pytest.raises(FilmError, match="audio"):
        verify_delivery(tmp_path / "silent_final.mp4", frames, recipe,
                        frames.seconds, audio_track=track["path"])


def test_verifier_rejects_altered_audio(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    craft = tmp_path / "vid.mp4"
    video_only_mp4(craft, frames.dir, frames.count)
    wrong = make_master(tmp_path / "wrong.wav", frames.seconds, tone=880)
    wrong_track = prepare_audio_track(wrong, frames.seconds,
                                      tmp_path / "w6x")
    good_track = prepare_audio_track(master, frames.seconds, tmp_path / "w6")
    muxed = tmp_path / "altered.mp4"
    mux_video_audio({"path": craft, "timed": True}, wrong_track, muxed,
                    FPS, frames.seconds)
    with pytest.raises(FilmError, match="reference|differs|substituted"):
        verify_delivery(muxed, frames, recipe, frames.seconds,
                        audio_track=good_track["path"])


def test_verifier_rejects_wrong_color_tags(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    craft = tmp_path / "color.mp4"
    video_only_mp4(craft, frames.dir, frames.count,
                   color_args=["-color_primaries", "bt709",
                               "-color_trc", "bt709",
                               "-colorspace", "bt709"])
    track = prepare_audio_track(master, frames.seconds, tmp_path / "w7")
    muxed = tmp_path / "color_mux.mp4"
    mux_video_audio({"path": craft, "timed": True}, track, muxed, FPS,
                    frames.seconds)
    with pytest.raises(FilmError, match="colour|color"):
        verify_delivery(muxed, frames, recipe, frames.seconds,
                        audio_track=track["path"])


# --- QUALIFIED_SERVICE protocol ---------------------------------------------

def test_service_export_then_import_protocol(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    recipe = make_encode_recipe("QUALIFIED_SERVICE", fmt)
    driver = get_driver("QUALIFIED_SERVICE")
    work = tmp_path / "svc"
    # Step 1: no local bytes — a manual export packet is written.
    with pytest.raises(ManualExportRequired):
        encode_delivery(frames, recipe, tmp_path / "svc.mp4",
                        driver=driver, master=master, work_dir=work)
    packet = read_canon(work / "manual_export_packet.json")
    assert packet["driver"] == "QUALIFIED_SERVICE"
    assert packet["frame_count"] == frames.count
    # Step 2: the external party returns packets (here: a real video-only
    # MP4 encoded outside the driver's own ffmpeg image2 path).
    returned = tmp_path / "returned.mp4"
    video_only_mp4(returned, frames.dir, frames.count)
    result = encode_delivery(frames, recipe, tmp_path / "svc.mp4",
                             driver=driver, master=master, work_dir=work,
                             import_path=returned)
    assert result["status"] == "COMPLETE"
    assert result["driver"] == "QUALIFIED_SERVICE"
    # The returned stream was made by FFmpeg in this test; an imported
    # export can never demonstrate NO_FFMPEG_ENCODING by itself.
    assert result["no_ffmpeg_encoding"] == "EXTERNAL_UNVERIFIED"


def test_service_import_rejects_garbage(tmp_path):
    frames, recipe, master, fmt = fixture(tmp_path)
    recipe = make_encode_recipe("QUALIFIED_SERVICE", fmt)
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"not a media file at all")
    with pytest.raises(FilmError):
        encode_delivery(frames, recipe, tmp_path / "svc2.mp4",
                        driver=get_driver("QUALIFIED_SERVICE"),
                        master=master, import_path=junk)


def test_service_import_rejects_wrong_pixels(tmp_path):
    """Imported packets that encode different frames fail the same
    verifier — the service never self-certifies."""
    frames, recipe, master, fmt = fixture(tmp_path)
    recipe = make_encode_recipe("QUALIFIED_SERVICE", fmt)
    other = tmp_path / "other_frames"
    make_delivery_frames(other, frames.count, seed=111)
    returned = tmp_path / "wrong.mp4"
    video_only_mp4(returned, other, frames.count)
    with pytest.raises(FilmError):
        encode_delivery(frames, recipe, tmp_path / "svc3.mp4",
                        driver=get_driver("QUALIFIED_SERVICE"),
                        master=master, import_path=returned)


# --- Build 2 integration ----------------------------------------------------

def test_build2_records_encoding_block(tmp_path):
    p = animation_project(tmp_path, shot_count=2, seconds=4)
    make_sequence(tmp_path / "seq_s001", count=48, size=SIZE, seed=10)
    make_sequence(tmp_path / "seq_s002", count=48, size=SIZE, seed=200)
    from engine.animation_assets import import_frame_sequence
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    approve_all(p)
    from engine.compiler import compile_final
    compile_final(p)
    folder, record = newest_build(p)
    encoding = record["encoding"]
    assert encoding["driver"] == "FFMPEG"
    assert encoding["delivery_profile"] == "MV_H264_AAC_V1"
    validate_encode_recipe(encoding["recipe"])
    assert encoding["verify_contract"] == VERIFY_CONTRACT
    assert encoding["audio_track"]["stream_copied"] is True
    assert encoding["no_ffmpeg_encoding"] == "NOT_DEMONSTRATED"
    assert encoding["clean"]["encode_digest"] \
        != encoding["subbed"]["encode_digest"]
    assert verify_build(folder)["valid"]
    # Replay still works end-to-end through the new pipeline.
    from engine.builds import replay_build
    out = tmp_path / "replay"
    replay_build(folder, out)
    assert (out / "MASTER_SUBBED.mp4").is_file()


def test_encode_build_cli_ffmpeg(tmp_path):
    p = animation_project(tmp_path, shot_count=2, seconds=4)
    make_sequence(tmp_path / "seq_s001", count=48, size=SIZE, seed=10)
    make_sequence(tmp_path / "seq_s002", count=48, size=SIZE, seed=200)
    from engine.animation_assets import import_frame_sequence
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    approve_all(p)
    from engine.compiler import compile_final
    compile_final(p)
    folder, record = newest_build(p)
    out = tmp_path / "enc" / "MASTER_SUBBED.mp4"
    result = encode_build_delivery(folder, "FFMPEG", out)
    assert result["status"] == "COMPLETE"
    assert result["driver"] == "FFMPEG"
    assert result["no_ffmpeg_encoding"] == "NOT_DEMONSTRATED"


def test_encode_build_service_roundtrip(tmp_path):
    p = animation_project(tmp_path, shot_count=2, seconds=4)
    make_sequence(tmp_path / "seq_s001", count=48, size=SIZE, seed=10)
    make_sequence(tmp_path / "seq_s002", count=48, size=SIZE, seed=200)
    from engine.animation_assets import import_frame_sequence
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    approve_all(p)
    from engine.compiler import compile_final
    compile_final(p)
    folder, record = newest_build(p)
    out_dir = tmp_path / "svc"
    out = out_dir / "MASTER_SUBBED.mp4"
    # No import -> WAITING_MANUAL_IMPORT with an export packet.
    result = encode_build_delivery(folder, "QUALIFIED_SERVICE", out)
    assert result["status"] == "WAITING_MANUAL_IMPORT"
    assert Path(result["export_packet"]).is_file()
    # A returned packet file is imported, muxed and verified.
    returned = tmp_path / "returned.mp4"
    video_only_mp4(returned, folder / "final_frames",
                   record["output_frames"])
    result = encode_build_delivery(folder, "QUALIFIED_SERVICE", out,
                                   import_path=returned)
    assert result["status"] == "COMPLETE"
    assert result["no_ffmpeg_encoding"] == "EXTERNAL_UNVERIFIED"


def test_encode_build_rejects_unavailable_driver(tmp_path):
    p = animation_project(tmp_path, shot_count=2, seconds=4)
    make_sequence(tmp_path / "seq_s001", count=48, size=SIZE, seed=10)
    make_sequence(tmp_path / "seq_s002", count=48, size=SIZE, seed=200)
    from engine.animation_assets import import_frame_sequence
    import_frame_sequence(p, "S001", folder=tmp_path / "seq_s001")
    import_frame_sequence(p, "S002", folder=tmp_path / "seq_s002")
    approve_all(p)
    from engine.compiler import compile_final
    compile_final(p)
    folder, _ = newest_build(p)
    if get_driver("NVIDIA_NATIVE").probe()["registry_state"] \
            == "UNAVAILABLE":
        with pytest.raises(FilmError, match="UNAVAILABLE"):
            encode_build_delivery(folder, "NVIDIA_NATIVE",
                                  tmp_path / "nv" / "out.mp4")


def test_encoders_cli(tmp_path, capsys):
    assert cli.main(["encoders", "--driver", "FFMPEG"]) == 0
    out = capsys.readouterr().out
    evidence = json.loads(out)
    assert evidence["registry_state"] in (
        "QUALIFIED_FOR_SCOPE", "UNAVAILABLE", "STALE", "DOCUMENTED_ONLY")
    assert cli.main(["encoders"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out.keys()) == set(DRIVER_NAMES)
    assert cli.main(["encoders", "--driver", "NOPE"]) == 1


@pytest.mark.skipif(shutil.which("gst-launch-1.0") is None,
                    reason="gst-launch-1.0 not installed on this host")
def test_gstreamer_real_encode(tmp_path):
    """Runs only where the GStreamer toolchain is actually installed."""
    frames, recipe, master, fmt = fixture(tmp_path)
    recipe = make_encode_recipe("GSTREAMER", fmt)
    result = encode_delivery(frames, recipe, tmp_path / "gst.mp4",
                             driver=get_driver("GSTREAMER"),
                             master=master)
    assert result["status"] == "COMPLETE"
    assert result["no_ffmpeg_encoding"] is True


def test_nothing_qualifies_from_names_alone(tmp_path):
    """The ADR rule: a documented name or an FFmpeg hwaccel flag is never
    evidence. On this host every non-FFmpeg driver is DOCUMENTED_ONLY or
    UNAVAILABLE — none can be QUALIFIED_FOR_SCOPE by naming."""
    evidence = probe_all()
    for name in ("NVIDIA_NATIVE", "VIDEOTOOLBOX_NATIVE", "GSTREAMER",
                 "QUALIFIED_SERVICE"):
        state = evidence[name]["registry_state"]
        assert state in {"UNAVAILABLE", "DOCUMENTED_ONLY"}
