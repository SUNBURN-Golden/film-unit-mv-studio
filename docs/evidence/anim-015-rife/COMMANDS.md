# anim-015-rife-onnx commands

Recorded from the working tree of `film/anim-015-rife-onnx` at source head
`ce66a7a039901cbd71ab3743ad2153c4b496452c` (changes uncommitted). No weight
download. onnxruntime is not installed. `rife_onnx` tests use an injected
FAKE session. CI installs `.[test]` only. This working tree was not pushed,
so there is no CI run for it.

```bash
/workspace/venvs/film312/bin/python scripts/anim015_rife_demo.py
```

Exit 0. `docs/evidence/anim-015-rife/demo.json`: `qualification_state`
UNQUALIFIED, `ran` false, `weights_state` MISSING, `no_ffmpeg_encoding`
false. `segment-quote --adapter rife_onnx` raised `CAPABILITY_UNAVAILABLE`
(onnxruntime absent). No blend fallback. Wall time 1872 ms, peak RSS 59424 kB.
No PNG or MP4 was written into the repo.

```bash
/workspace/venvs/film312/bin/python -m pytest -q tests/test_anim_015_rife_onnx.py
```

10 passed in 6.71s.

```bash
/workspace/venvs/film312/bin/python -m compileall -q engine app
/workspace/venvs/film312/bin/python -m pytest -q tests -k "not media and not qc_registration and not registration_safety and not review_dependencies and not gemini_video_end_to_end and not provider_fallback_reuses"
```

compileall exit 0. Pytest: 2334 passed, 3 skipped, 25 deselected in 1724.21s.
