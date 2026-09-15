from pathlib import Path
import argparse
import json
import sys
from .core import FilmError, init_project, lock_production, project_mutex, read
from .audio import analyze, synth_test_audio
from .production import make_package, import_frame
from .pipeline import compile_project, prepare
from .budget import approve


def main(argv=None):
    parser = argparse.ArgumentParser(prog="filmunit")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("name")
    init.add_argument("--audio", required=True)
    init.add_argument("--brief", required=True, help="UTF-8 creative brief file")
    init.add_argument("--lyrics")
    init.add_argument("--root", default="projects")
    demo = sub.add_parser("demo", help="Create a clearly labelled synthetic test project")
    demo.add_argument("--root", default="projects")
    demo.add_argument("--name", default="pipeline_demo")
    demo.add_argument("--seconds", type=float, default=65)
    benchmark = sub.add_parser("benchmark", help="Prepare three actual-animation tests from one image; no LOCK or paid calls")
    benchmark.add_argument("--reference", required=True)
    benchmark.add_argument("--name", default="animation_benchmark")
    benchmark.add_argument("--root", default="projects")
    for name in ["compile-preview", "compile-final", "lyrics-prepare", "lyrics-import", "migrate", "builds", "import-asset", "split-shot", "merge-shots", "move-cut", "snap-cut"]:
        p = sub.add_parser(name)
        p.add_argument("project")
        if name in {"compile-preview", "compile-final"}:
            p.add_argument("--quality", choices=["draft", "final"], default="draft" if name == "compile-preview" else "final")
        if name == "lyrics-import":
            p.add_argument("--file", required=True, help="UTF-8 lyrics_timed.json with measured or reviewed timing")
            p.add_argument("--reviewer", default="")
        if name == "import-asset":
            p.add_argument("shot")
            p.add_argument("source")
            p.add_argument("--kind", choices=["draft", "final"], default="draft")
            p.add_argument("--reviewer", default="")
            p.add_argument("--evidence", default="", help="Final clip review evidence")
            p.add_argument("--source-in-ms", type=int, default=0)
        if name in {"split-shot", "move-cut", "snap-cut"}:
            p.add_argument("shot")
        if name in {"split-shot", "move-cut"}:
            p.add_argument("--at-ms", type=int, required=True)
        if name == "merge-shots":
            p.add_argument("left")
            p.add_argument("right")
        if name == "snap-cut":
            p.add_argument("--to", choices=["beat", "onset"], default="beat")
            p.add_argument("--around-ms", type=int)
    verify = sub.add_parser("verify-build")
    verify.add_argument("build_dir")
    replay = sub.add_parser("replay-build")
    replay.add_argument("build_dir")
    replay.add_argument("--output", required=True)
    for name in ["analyze", "package", "lock", "estimate", "approve", "compile", "import-frame", "economy-init", "work-status", "edit-take", "claim-job"]:
        p = sub.add_parser(name, help="Legacy shot generation/resume (may call paid renderers); use compile-preview for a local full-song build" if name == "compile" else None)
        p.add_argument("project")
        if name in {"estimate", "compile"}:
            p.add_argument("--seconds", type=float)
            p.add_argument("--renderer", choices=["mock", "manual", "openart", "fal", "economy"], default="mock")
            p.add_argument("--quality", choices=["draft", "final"], default="final")
        if name == "compile":
            p.add_argument("--output")
        if name == "package":
            p.add_argument("--preset", help="Production preset directory name (e.g. concrete_glide); defaults to neutral")
        if name == "lock":
            p.add_argument("--reviewer", required=True)
            p.add_argument("--mock-only", action="store_true")
        if name == "approve":
            p.add_argument("--estimate-id", required=True)
        if name == "import-frame":
            p.add_argument("shot")
            p.add_argument("image")
        if name == "edit-take":
            p.add_argument("shot")
            p.add_argument("--source-in-ms", type=int, required=True)
            p.add_argument("--notes", required=True)
        if name == "claim-job":
            p.add_argument("job")
    a = parser.parse_args(argv)
    try:
        if a.command in {"compile-preview", "compile-final"}:
            from .compiler import compile_final, compile_preview
            compile_fn = compile_preview if a.command == "compile-preview" else compile_final
            result = compile_fn(a.project, quality=a.quality,
                progress=lambda i, n, s: print(f"[{i}/{n}] {s}", file=sys.stderr, flush=True))
        elif a.command == "lyrics-prepare":
            from .lyrics import prepare_lyrics
            with project_mutex(a.project):
                result = prepare_lyrics(a.project)
        elif a.command == "lyrics-import":
            from .lyrics import save_timing
            with project_mutex(a.project):
                result = save_timing(a.project, json.loads(Path(a.file).read_text(encoding="utf-8")), reviewer=a.reviewer)
        elif a.command == "migrate":
            from .schema import migrate_project
            result = migrate_project(a.project)
        elif a.command == "builds":
            from .builds import list_builds
            result = list_builds(a.project)
        elif a.command == "verify-build":
            from .builds import verify_build
            result = verify_build(a.build_dir)
        elif a.command == "replay-build":
            from .builds import replay_build
            result = replay_build(a.build_dir, a.output)
        elif a.command == "import-asset":
            from .compiler import import_asset
            result = import_asset(a.project, a.shot, a.source, kind=a.kind,
                reviewer=a.reviewer, evidence=a.evidence, source_in_ms=a.source_in_ms)
        elif a.command == "split-shot":
            from .timeline import split_shot
            result = split_shot(a.project, a.shot, a.at_ms)
        elif a.command == "merge-shots":
            from .timeline import merge_shots
            result = merge_shots(a.project, a.left, a.right)
        elif a.command == "move-cut":
            from .timeline import move_cut
            result = move_cut(a.project, a.shot, a.at_ms)
        elif a.command == "snap-cut":
            from .timeline import snap_cut
            result = snap_cut(a.project, a.shot, a.to, a.around_ms)
        elif a.command == "economy-init":
            from .economy import initialize
            result = initialize(a.project)
        elif a.command == "work-status":
            from .work_queue import status
            result = status(a.project)
        elif a.command == "edit-take":
            from .takes import select_window
            with project_mutex(a.project):
                result = select_window(a.project, a.shot, a.source_in_ms, a.notes)
        elif a.command == "claim-job":
            from .openart_bridge import claim_submission
            result = claim_submission(a.project, a.job)
        elif a.command == "init":
            result = str(init_project(a.root, a.name, a.audio, Path(a.brief).read_text(), Path(a.lyrics).read_text() if a.lyrics else ""))
        elif a.command == "benchmark":
            from .benchmark import make_benchmark
            result = {"project": str(make_benchmark(a.root, a.name, a.reference)), "status": "AWAITING_REVIEW_AND_PROVIDER_CONNECTION", "paid_generations": 0}
        elif a.command == "demo":
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                audio = synth_test_audio(Path(tmp) / "test.wav", a.seconds)
                p = init_project(a.root, a.name, audio, "Synthetic pipeline acceptance fixture. Neutral visual placeholders; director review required.", synthetic=True)
            analyze(p)
            make_package(p)
            lock_production(p, "automated synthetic acceptance fixture", mock_only=True)
            result = {"project": str(p), "audio": "synthetic test signal", "locked_for": "mock only"}
        elif a.command == "analyze":
            result = analyze(a.project)
        elif a.command == "package":
            result = {"shots": len(make_package(a.project, preset=a.preset))}
        elif a.command == "lock":
            lock_production(a.project, a.reviewer, a.mock_only)
            result = {"locked": True, "mock_only": a.mock_only}
        elif a.command == "estimate":
            result = prepare(a.project, a.seconds, a.renderer, a.quality)[4]
        elif a.command == "approve":
            estimate = read(Path(a.project) / "render/estimate.json")
            if estimate["estimate_id"] != a.estimate_id:
                raise FilmError("Estimate ID does not match the current estimate")
            approve(a.project, estimate)
            result = {"approved": a.estimate_id}
        elif a.command == "import-frame":
            import_frame(a.project, a.shot, a.image)
            result = {"updated": a.shot, "production_lock": "requires review"}
        else:
            result = compile_project(a.project, a.seconds, a.renderer, a.quality, a.output,
                progress=lambda i, n, s: print(f"[{i}/{n}] {s}", file=sys.stderr, flush=True))
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    except (FilmError, ValueError, OSError) as e:
        print(f"FILM UNIT: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
