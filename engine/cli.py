from pathlib import Path
import argparse
import json
import sys
from .core import FilmError, init_project, lock_production, read
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
    for name in ["analyze", "package", "lock", "estimate", "approve", "compile", "import-frame"]:
        p = sub.add_parser(name)
        p.add_argument("project")
        if name in {"estimate", "compile"}:
            p.add_argument("--seconds", type=float)
            p.add_argument("--renderer", choices=["mock", "manual", "openart", "fal"], default="mock")
            p.add_argument("--quality", choices=["draft", "final"], default="final")
        if name == "compile":
            p.add_argument("--output")
        if name == "lock":
            p.add_argument("--reviewer", required=True)
            p.add_argument("--mock-only", action="store_true")
        if name == "approve":
            p.add_argument("--estimate-id", required=True)
        if name == "import-frame":
            p.add_argument("shot")
            p.add_argument("image")
    a = parser.parse_args(argv)
    try:
        if a.command == "init":
            result = str(init_project(a.root, a.name, a.audio, Path(a.brief).read_text(), Path(a.lyrics).read_text() if a.lyrics else ""))
        elif a.command == "benchmark":
            from .benchmark import make_benchmark
            result = {"project": str(make_benchmark(a.root, a.name, a.reference)), "status": "AWAITING_REVIEW_AND_PROVIDER_CONNECTION", "paid_generations": 0}
        elif a.command == "demo":
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                audio = synth_test_audio(Path(tmp) / "test.wav", a.seconds)
                p = init_project(a.root, a.name, audio, "Pipeline acceptance test. Two parallel subject placeholders, restrained static framing, warm gray paper and teal. This is not the final creative treatment for 물 좀 주소.", synthetic=True)
            analyze(p)
            make_package(p)
            lock_production(p, "automated synthetic acceptance fixture", mock_only=True)
            result = {"project": str(p), "audio": "synthetic test signal", "locked_for": "mock only"}
        elif a.command == "analyze":
            result = analyze(a.project)
        elif a.command == "package":
            result = {"shots": len(make_package(a.project))}
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
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (FilmError, ValueError, OSError) as e:
        print(f"FILM UNIT: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
