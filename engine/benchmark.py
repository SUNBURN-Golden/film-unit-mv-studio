"""Create a reviewable 3 x 5s motion comparison, without LOCK or paid calls."""
from pathlib import Path
import shutil
import tempfile
import numpy as np
import soundfile as sf
from .core import atomic_text, init_project, read, write
from .audio import analyze
from .production import make_package, generate_storyboard

CASES = [
    ("걷기", "Release the cup handle, withdraw the right hand, then take two natural steps toward screen-left. Keep both feet in frame. Animate alternating leg contact, subtle weight transfer and arm swing. No foot sliding, pose teleportation or camera movement."),
    ("컵을 드는 손동작", "Keep the body in place. With the hand already beside the cup, close the fingers around its handle, lift it about 15 centimetres, pause, then set it back on the same table position. Preserve five fingers, handle shape and stable water; no object morphing."),
    ("표정 변화", "Keep the body and camera in place. Animate one natural blink, a slight shift of the eyes toward the cup, and a subtle softening of the mouth into a faint smile. Maintain facial proportions and hairstyle. No talking, exaggerated grin or camera zoom."),
]


def make_benchmark(root, name, reference):
    reference = Path(reference).resolve()
    if not reference.is_file():
        raise FileNotFoundError(reference)
    brief = "Renderer comparison only: three independent 5-second 2D animation tests. Same starting frame, camera and style. This is not the final MV or an approved final character. Silent synthetic audio is only a timing fixture."
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "silent_test.wav"
        sf.write(audio, np.zeros(15 * 16000, dtype=np.float32), 16000)
        p = init_project(root, name, audio, brief, synthetic=True)
    analyze(p)
    shots = make_package(p)
    if len(shots) != 3:
        raise ValueError("Benchmark must have exactly three five-second shots")
    for shot, (title, instruction) in zip(shots, CASES):
        shot.update(sequence="SEQ01", description=title, characters=["CHAR_TEST"], locations=["LOC_TEST_ROOM"],
                    composition="Full-body three-quarter view, table and cup at screen-right; preserve the supplied frame",
                    storyboard_kind="generated_test_reference", status="storyboard")
        shot["motion"].update(instruction=instruction, complexity="medium")
        shutil.copyfile(reference, p / shot["references"][0])
    write(p / "manifest/shots.json", shots)
    write(p / "manifest/sequence.json", [{"id": "SEQ01", "in_ms": 0, "out_ms": 15000,
          "title": "Independent motion benchmarks", "boundary_source": "Explicit test intervals against measured 15000ms silent audio"}])
    write(p / "bible/characters.yaml", {"draft": True, "characters": [{"id": "CHAR_TEST",
          "invariants": ["same adult male face and short black hair", "white shirt, black tie, dark trousers", "no glasses"],
          "reference_images": ["storyboard/S001.png"]}], "note": "Test character only; not the final movie's CHAR_A."})
    write(p / "bible/locations.yaml", {"draft": True, "locations": [{"id": "LOC_TEST_ROOM",
          "invariants": ["same room, table, window, chair, cup and plant as reference"], "reference_images": ["storyboard/S001.png"]}]})
    style = read(p / "bible/style_bible.yaml")
    style["note"] = "Follow the supplied test artwork, including its actual muted colors. Original brief palette is a direction, not measured pixel colors."
    write(p / "bible/style_bible.yaml", style)
    atomic_text(p / "bible/story.md", "# Motion comparison draft\n\n" + brief + "\n\n" + "\n\n".join(f"{title}: {instruction}" for title, instruction in CASES))
    generate_storyboard(p)
    template = Path(__file__).resolve().parents[1] / "templates/fal_wan_turbo.json"
    write(p / "render/fal_config.json", read(template))
    write(p / "render/comparison_plan.json", {
        "status": "AWAITING_REVIEW_AND_PROVIDER_CONNECTION", "video_generations_completed": 0,
        "cases": [{"shot": shot["id"], "title": title, "duration_ms": 5000} for shot, (title, _) in zip(shots, CASES)],
        "fal": {"resolution": "720p", "first_batch_usd": 0.30, "with_two_retries_per_shot_usd": 0.90},
        "openart_pixverse_v6": {"resolution": "720p", "reference_estimate_credits": 210,
            "with_two_retries_per_shot_credits": 630, "requires_fresh_exact_quote": True},
        "qc": ["actual limb/facial movement", "identity and clothing", "hand/cup anatomy", "camera remains fixed", "background/style consistency"],
    })
    return p
