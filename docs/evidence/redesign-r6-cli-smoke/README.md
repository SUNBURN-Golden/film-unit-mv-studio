# R6 subscription-CLI driver — one real run on synthetic frames

What this is: `smoke_real.py` builds a throwaway six-shot project, imports an ffmpeg
**`testsrc` test pattern** as the "take", sets `production.rewrite_driver: claude_cli`
and runs `conti-loop` once. The calls went to the real Claude Code CLI 2.1.290
(`claude -p`) on this box's claude.ai subscription login (`authMethod: claude.ai`,
no `ANTHROPIC_*` variable set), 2026-10-15, three times while the driver was fixed.

What it shows (real, not mocked):

- the flags work against the real CLI: `--tools Read --add-dir <frames>` lets the model
  open the five sampled JPEGs, and `--tools ""` answers the rewrite; stdin carries the prompt;
- the JSON envelope (`type: result`, `result`, `modelUsage`) is what the parser reads;
  the model that answered is recorded (`claude-fable-5-1`);
- the judge really looked at the frames: its reasons describe the colour bars and the
  counter digit, and it FAILed all three criteria (a test card has no coat or cup) —
  the correct verdict for this input;
- validation is a real gate. Run 1 (`run1-rejected-schema-version/`) was rejected because
  the schema example shown to the model had no `schema_version`; run 2
  (`run2-rejected-length/`) because an `open_questions` entry was longer than 300
  characters. Both rejections stored nothing and kept the raw answer. The request text
  now carries `schema_version` and the length limits (`engine/conti.py`), which also
  fixes the manual `conti-request` path. Run 3 (`run3-applied/`) was accepted and stored.

What it does NOT show:

- anything about a real generated shot — the "take" is a test pattern, so this says
  nothing about whether the judge is lenient or strict on real footage, or whether a
  rewrite improves the next real take (that is REDESIGN_W00 R5);
- that the rewrite stays minimal: the model changed 15 conti fields for a test card
  (`conti_changed` in the run-3 evidence). It kept `acceptance_criteria` unchanged, as
  the driver requires, but "change only what is needed" is not enforced beyond that;
- subscription cost or limits: `modelUsage` is recorded raw; no allowance was measured.
