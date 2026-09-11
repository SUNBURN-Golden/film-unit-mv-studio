# OpenArt bridge / Work operator guide

## Boundary

OpenArt is connected in Work. The local Python process cannot invoke another application's authenticated MCP tools directly. `OpenArtRenderer` therefore exchanges explicit request/receipt files with Work. It does not implement a fictitious REST endpoint. This delivery does not include paid provider generations.

## One batch, three real shots

1. Use Work to create real first frames and character/location references. Replace placeholders, complete the story and shot descriptions, then LOCK for production. Keep the approved first frame as the literal first frame of `image2video`.
2. For the three-shot integration test only, select three animated shots and set the remaining shots to `renderer: mock`. This produces a hybrid technical test, not a finished animation MV. For the finished pilot or full song, supply actual animation for every animated shot through OpenArt or Manual; reserve STATIC for intentional editorial holds. Any change to `manifest/shots.json` requires review before a new LOCK.
3. Query `openart_model_list`, then `openart_model_form_get` for each selected model/mode. Never infer accepted fields from a marketing model name. `templates/` contains a 2026-09-11 snapshot for reference only.
4. Make the approved images available through the provider's supported asset-upload flow. Obtain each reference object through the available upload/metadata tools. Verify that the referenced image matches the local approved first frame. Never fabricate an uploaded asset ID or URL.
5. Build model form parameters including exact duration, resolution, aspect ratio and one output. Disable `generateAudio`. Ask `openart_model_cost` to price that exact configuration. Provider duration can exceed a cut's duration; the compiler trims only after generation.
6. Call `engine.openart_bridge.register_quote(...)` with structured form, uploaded reference and cost evidence for each shot. Pass `extra_params` for the exact priced duration/resolution. Quotes expire after one hour. Use the same model choice in each shot's `renderer` field or configure only compatible AUTO candidates.
7. `python -m engine.cli estimate PROJECT --seconds 30 --renderer openart --quality draft` shows the concrete total, including all possible retries. The user approves that batch once. Record it using the UI or `approve --estimate-id ID`.
8. Compile. The renderer writes `render/requests/<job_id>.json` for every selected shot. Other local clips can render in the same run. Compilation returns AWAITING_REVIEW_OR_RENDER without claiming an output.
9. Work rechecks each exact cost immediately before submission. If the price changed, update the quote and get approval for the revised concrete batch. Otherwise submit `arguments` through `openart_generate_video`. Do not submit any request with an existing `history_id`; use it to recover the prior job.
10. Immediately call `record_submission(project, job_id, returned_history_id)`. In Work, provider result cards are asynchronous. Follow the host's result-card instructions; do not busy-poll or submit again. A later resume can use `openart_creation_get` to recover a completed known job as allowed by that tool's instructions.
11. Retrieve completed video bytes through a supported media/download path and call `import_completed(project, job_id, history_id, local_clip_path)`. The receipt binds the provider history and clip hash. For a confirmed FAILED/CANCELLED job, write that terminal state with the same job ID and provider error in its response JSON.
12. Resume compilation. Inspect sampled frames and actual motion. Save a semantic review in the panel or write the documented review JSON. If all criteria pass, assembly continues. If a criterion fails, the next bounded attempt gets the specific correction. Awaiting review is not a failure and does not consume a retry.

## Review JSON

Use the exact normalized clip hash and production ID in `qc/report.json`. Fields below are schematic; the actual scores must come from inspection.

```json
{
  "clip_sha256": "<actual hash from report>",
  "production_id": "<actual production digest>",
  "reviewer": "<human or vision reviewer identifier>",
  "notes": "<concrete observations across reference and sampled frames>",
  "scores": {
    "character_identity": null,
    "style": null,
    "composition": null,
    "palette": null,
    "camera": null,
    "background": null,
    "props": null,
    "unwanted_text": null,
    "anatomy": null,
    "motion": null
  }
}
```

Null scores pause. Every dimension must be at least 85 by default. Review files must be named as requested in `qc/report.json`. No canned high scores are supplied for real clips.

## Manual alternative

Generate a clip externally from the locked first frame. Upload it as `render/manual/S017_a0.mp4`; subsequent attempts use `_a1` and `_a2`. Choose Manual in the panel. These files are treated as external clips requiring semantic review, not as verified proof of a particular AI model. External generation costs are outside the compiler's provider ledger.

## Failure and cost semantics

- Never refund a local reservation based only on a network error.
- Never resubmit when the previous submission outcome is unknown. Reconcile provider history first.
- After two configured retries, stop that shot and show evidence.
- Do not replace a failed shot with an unrelated placeholder while labelling the MV complete.
- Provider-native totals remain authoritative; this local ledger controls only jobs initiated through this project workflow.
