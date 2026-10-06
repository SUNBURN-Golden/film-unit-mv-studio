"""Executed by the frozen EXE itself, not the build machine's Python."""
from pathlib import Path
import json
import os
import socket
from unittest.mock import patch


def run(folder):
    from engine.audio import analyze, synth_test_audio
    from engine.builds import verify_build
    from engine.compiler import compile_preview, compile_final
    from engine.core import FilmError, digest, ffmpeg, init_project, probe, read, write
    from engine.lyrics import prepare_lyrics, save_timing
    from engine.production import make_package, load_preset
    from desktop.runtime import configure_project_font, resource_root

    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    # Any accidental provider/network request makes the compile smoke fail.
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Offline smoke forbids network")):
        source = synth_test_audio(folder / "원음 테스트.wav", seconds=6)
        project = init_project(folder, "smoke_project", source,
                               "한글 폴더와 자막의 Windows 검증", "한 곡에서 한 편으로\nMusic video compiler\n", synthetic=True)
        configure_project_font(project)
        config = read(project / "project.yaml")
        if not config.get("subtitles", {}).get("font_file"):
            raise RuntimeError("Korean font was not included in this distribution")
        config["format"].update(width=320, height=240, crf=28)
        write(project / "project.yaml", config)
        analysis = analyze(project)
        assert analysis["duration_ms"] == 6000
        assert load_preset("water_please")["style"]
        make_package(project, target_shot_ms=3000)
        lyrics = prepare_lyrics(project)
        rows = [r for r in lyrics["rows"] if r["kind"] == "lyric"]
        lyrics["cues"] = [{"id": f"C{i:03}", "source_row_id": r["id"], "text": r["text"],
                           "start_ms": i * 3000, "end_ms": i * 3000 + 2800} for i, r in enumerate(rows)]
        save_timing(project, lyrics)  # Synthetic fixture; never claim human review.
        result = compile_preview(project)
        build = Path(result["build_dir"])
        inventory = verify_build(build)
        assert inventory["valid"], inventory
        record = read(build / "build.json")
        assert record["lyrics"]["font_report"]["status"] == "verified"
        assert digest(project / "input/master.wav") == digest(source)
        assert "한 곡에서 한 편으로" in (build / "lyrics.srt").read_text(encoding="utf-8")
        video = probe(result["output"])
        assert abs(float(video["format"]["duration"]) - 6) < 0.1
        assert {s["codec_type"] for s in video["streams"]} == {"video", "audio"}
        # Rendered pixels must differ from clean output in the subtitle region.
        frames = []
        for path in (result["clean"], result["output"]):
            frames.append(ffmpeg(["-ss", "1", "-i", path, "-vf", "crop=320:80:0:160", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]))
        import numpy as np
        difference = np.abs(np.frombuffer(frames[0], dtype=np.uint8).astype(int) - np.frombuffer(frames[1], dtype=np.uint8).astype(int))
        assert (difference > 35).sum() > 100, "Subtitle pixels were not rendered"
        try:
            compile_final(project)
        except FilmError:
            pass
        else:
            raise AssertionError("Packaging must not bypass Final approval")

    # The panels are loaded by name at run time; a module missing from the bundle would only fail on its tab.
    # app.animation_ui carries the FRAME_ANIMATION_V1 panels (paths A/B/C) added by ANIM-011/012.
    import importlib
    modules = ["app.models_ui", "app.handoff_ui", "app.animation_ui"]
    for name in modules:
        importlib.import_module(name)

    # AppTest executes the actual Streamlit script, unlike the health endpoint.
    from streamlit.testing.v1 import AppTest
    os.environ["FILM_UNIT_PROJECTS"] = str(folder / "empty-ui-projects")
    app = AppTest.from_file(str(resource_root() / "app/control_panel.py")).run(timeout=90)
    assert not app.exception, [str(e) for e in app.exception]
    assert any("프로젝트 만들기" in button.label for button in app.button)
    # With a project selected every tab runs, including the model picker and the browser hand-off panels.
    os.environ["FILM_UNIT_PROJECTS"] = str(folder)
    with_project = AppTest.from_file(str(resource_root() / "app/control_panel.py")).run(timeout=90)
    assert not with_project.exception, [str(e) for e in with_project.exception]
    assert any("웹사이트 연결" in header.value for header in with_project.subheader), "hand-off tab did not render"
    assert any("AI 모델 선택" in header.value for header in with_project.subheader), "model picker did not render"
    report = {"status": "PASS", "frozen": bool(getattr(__import__("sys"), "frozen", False)),
              "duration_ms": analysis["duration_ms"], "build_inventory": inventory,
              "subtitle_glyphs": "verified", "subtitle_pixels": int((difference > 35).sum()),
              "original_audio_sha256": digest(source), "provider_calls": 0,
              "app_script_executed": True, "project_tabs_rendered": True, "app_modules": modules, "final_gate_preserved": True,
              "preview": result["output"]}
    (folder / "smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
