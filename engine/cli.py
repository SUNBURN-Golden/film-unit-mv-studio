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
    init.add_argument("--aspect", choices=["4:3", "16:9", "9:16"], default="4:3", help="Output canvas; Gemini Veo needs 16:9 or 9:16")
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
    anim_init = sub.add_parser("animation-init", help="Explicitly convert a LEGACY_MV project to FRAME_ANIMATION_V1 with metadata backup; never runs implicitly")
    anim_init.add_argument("project")
    anim_init.add_argument("--profile", choices=["frame-animation-v1"], default="frame-animation-v1")
    seq = sub.add_parser("import-sequence", help="FRAME_ANIMATION_V1: import an external frame sequence for one shot from an index or folder; registers a draft revision")
    seq.add_argument("project")
    seq.add_argument("shot")
    group = seq.add_mutually_exclusive_group(required=True)
    group.add_argument("--index", help="JSON index with an explicit frames order and per-member sha256")
    group.add_argument("--folder", help="Folder of PNG members ordered lexically by filename")
    seq.add_argument("--asset-id", help="Existing A0001-style asset id to revise")
    seq.add_argument("--note", default="")
    asset = sub.add_parser("import-animation-asset", help="FRAME_ANIMATION_V1: import a layer/mask/replacement drawing/rig spec as a draft revision")
    asset.add_argument("project")
    asset.add_argument("--kind", required=True, help="LAYER_RGBA, MASK, REPLACEMENT_DRAWING or RIG_SPEC")
    asset.add_argument("--file", required=True, help="Source PNG, or the rig spec JSON for RIG_SPEC")
    asset.add_argument("--asset-id", help="Existing A0001-style asset id to revise")
    asset.add_argument("--pivot", default="0,0", help="Layer/replacement pivot as integer x,y in asset pixels")
    asset.add_argument("--crop-origin", default="0,0", help="Crop origin as integer x,y")
    asset.add_argument("--z-order", type=int, default=0)
    asset.add_argument("--target", help="MASK: target pin asset_id:revision:sha256")
    asset.add_argument("--region", help="MASK region x,y,w,h in target canvas; default whole canvas")
    asset.add_argument("--channel", choices=["ALPHA", "LUMINANCE"], default="LUMINANCE")
    asset.add_argument("--replaces", help="REPLACEMENT_DRAWING: pin it replaces asset_id:revision:sha256")
    asset.add_argument("--frame", type=int, help="REPLACEMENT_DRAWING declared first frame")
    asset.add_argument("--note", default="")
    plan_cmd = sub.add_parser("shot-plan", help="FRAME_ANIMATION_V1: validate and store a shot's animation_shot_plan (C-path layers/camera)")
    plan_cmd.add_argument("project")
    plan_cmd.add_argument("shot")
    plan_cmd.add_argument("--file", required=True, help="Plan JSON document")
    composite_cmd = sub.add_parser("compile-shot", help="FRAME_ANIMATION_V1: composite one shot's C-path plan into a COMPOSITE_SEQUENCE draft")
    composite_cmd.add_argument("project")
    composite_cmd.add_argument("shot")
    composite_cmd.add_argument("--instance", help="Instance id when a shot has several entries")
    listing_anim = sub.add_parser("animation-assets", help="List registered animation assets and shot assignments")
    listing_anim.add_argument("project")
    check_anim = sub.add_parser("animation-validate", help="FRAME_ANIMATION_V1: check the edit timeline and per-shot sequence coverage")
    check_anim.add_argument("project")
    work = sub.add_parser("animation-packets", help="FRAME_ANIMATION_V1: write A-path work packets for manual hand-off; no provider contact, no consumer UI automation")
    work.add_argument("project")
    work.add_argument("--shots", help="Comma-separated shot IDs; default all timeline shots with an A segment")
    work.add_argument("--wave", help="Only shots of one declared production wave")
    work.add_argument("--target", help="JSON file declaring what the target site accepts: {id, accepts, max_images}")
    ctrl = sub.add_parser("import-control", help="FRAME_ANIMATION_V1: import one produced image explicitly assigned to cut/role/frame/asset; records DRAFT")
    ctrl.add_argument("project")
    ctrl.add_argument("shot")
    ctrl.add_argument("--file", required=True, help="Produced PNG image")
    ctrl.add_argument("--role", required=True,
                      choices=["keypose", "breakdown", "pose", "layout",
                               "first_frame", "inbetween"],
                      help="Explicit assignment; nothing is inferred from the file name")
    ctrl.add_argument("--frame", required=True, type=int,
                      help="Cut-local frame index the image is assigned to")
    ctrl.add_argument("--references", default="",
                      help="Comma-separated pins asset_id:revision:sha256 the image was conditioned on")
    ctrl.add_argument("--asset-id", help="Existing A0001-style asset id to revise")
    ctrl.add_argument("--note", default="")
    draft_commit = sub.add_parser("commit-draft-frames", help="FRAME_ANIMATION_V1: assemble the shot's packet-verified draft frames into a FRAME_SEQUENCE draft")
    draft_commit.add_argument("project")
    draft_commit.add_argument("shot")
    draft_commit.add_argument("--asset-id", help="Existing A0001-style asset id to revise")
    draft_commit.add_argument("--note", default="")
    seg_quote = sub.add_parser("segment-quote", help="ANIM-010 B path: capability preflight + quote for one plan segment through the declared adapter (fake_segment only is wired; FAKE/UNQUALIFIED)")
    seg_quote.add_argument("project")
    seg_quote.add_argument("shot")
    seg_quote.add_argument("--start", type=int, required=True, help="Segment start frame (cut-local)")
    seg_quote.add_argument("--end", type=int, required=True, help="Segment end frame, exclusive")
    seg_quote.add_argument("--adapter", default="fake_segment")
    seg_submit = sub.add_parser("segment-submit", help="ANIM-010 B path: approve the exact quote, reserve the ledger amount and submit one job; same-input resume reuses the job identity")
    seg_submit.add_argument("project")
    seg_submit.add_argument("shot")
    seg_submit.add_argument("--start", type=int, required=True)
    seg_submit.add_argument("--end", type=int, required=True)
    seg_submit.add_argument("--adapter", default="fake_segment")
    seg_submit.add_argument("--approver", required=True, help="Human approving this exact quote")
    seg_submit.add_argument("--quote-id", required=True, help="The quote id being approved; a changed quote must be re-approved")
    seg_submit.add_argument("--cap", type=float, required=True, help="Approved spending ceiling for the quote's unit")
    seg_submit.add_argument("--max-retries", type=int, default=0, help="Cost-cap bound on attempts; not proof a paid call is safe to resubmit")
    seg_reconcile = sub.add_parser("segment-reconcile", help="ANIM-010 B path: explicit status query on a job's existing identity; the only way to resolve UNKNOWN")
    seg_reconcile.add_argument("project")
    seg_reconcile.add_argument("job")
    seg_reconcile.add_argument("--adapter", default="fake_segment")
    seg_cancel = sub.add_parser("segment-cancel", help="ANIM-012 B path: request cancellation of a job — a request until the provider confirms termination; a completion confirmed first is kept for verification")
    seg_cancel.add_argument("project")
    seg_cancel.add_argument("job")
    seg_cancel.add_argument("--adapter", default="fake_segment")
    seg_import = sub.add_parser("segment-import", help="ANIM-010 B path: verify a returned clip (endpoint rule, length, hashes) and stage cut-local members; a longer clip needs --source-start/--source-count")
    seg_import.add_argument("project")
    seg_import.add_argument("job")
    seg_import.add_argument("--adapter", default="fake_segment")
    seg_import.add_argument("--source-start", type=int, default=0, help="First usable source frame of the returned clip")
    seg_import.add_argument("--source-count", type=int, help="Explicit used range length when the clip is longer than needed")
    seg_commit = sub.add_parser("segment-commit", help="ANIM-010 B path: assemble the shot's verified segment imports into a DRAFT FRAME_SEQUENCE")
    seg_commit.add_argument("project")
    seg_commit.add_argument("shot")
    seg_commit.add_argument("--asset-id", help="Existing A0001-style asset id to revise")
    seg_commit.add_argument("--note", default="")
    seg_jobs = sub.add_parser("segment-jobs", help="ANIM-010 B path: list segment job records (FAKE provider only)")
    seg_jobs.add_argument("project")
    seg_jobs.add_argument("--shot")
    seg_ctrl = sub.add_parser("segment-control", help="ANIM-010 B path: import one conditioning control image (keypose/breakdown/pose) for a B segment; records DRAFT, fills no member")
    seg_ctrl.add_argument("project")
    seg_ctrl.add_argument("shot")
    seg_ctrl.add_argument("--file", required=True, help="Conditioning PNG image")
    seg_ctrl.add_argument("--role", required=True,
                          choices=["keypose", "breakdown", "pose", "layout"])
    seg_ctrl.add_argument("--frame", required=True, type=int,
                          help="Cut-local frame the control conditions; == length only for a forced end anchor")
    seg_ctrl.add_argument("--references", default="",
                          help="Comma-separated pins asset_id:revision:sha256")
    seg_ctrl.add_argument("--asset-id", help="Existing A0001-style asset id to revise")
    seg_ctrl.add_argument("--note", default="")
    seg_fake = sub.add_parser("segment-fake", help="ANIM-010 B path: show or configure the FAKE provider (local test double; behaviours like lost_ack are simulation switches, never a real provider)")
    seg_fake.add_argument("project")
    seg_fake.add_argument("--set", dest="fake_set",
                          help="JSON object merged into the fake provider's settings/behaviors")
    arch = sub.add_parser("archive-create", help="ANIM-013: pack a folder of PNGs into a sealed FAV1 archive on a local backend")
    arch.add_argument("source", help="Folder of PNG members")
    arch.add_argument("--root", required=True, help="Archive backend root (objects/ + manifests/)")
    arch.add_argument("--profile", choices=["LOCAL_FULL", "DRIVE_BOUNDED"], default="LOCAL_FULL")
    arch.add_argument("--min-level", choices=["UPLOADED_UNVERIFIED", "UPLOAD_HASH_MATCHED", "FULL_READBACK"], default="UPLOAD_HASH_MATCHED")
    rst = sub.add_parser("archive-restore", help="ANIM-013: verified restore of an archive through the bounded workspace")
    rst.add_argument("manifest", help="Path to a storage_archive manifest JSON")
    rst.add_argument("dest", help="Destination folder (created; never overwritten)")
    rst.add_argument("--root", help="Archive backend root; defaults to the manifest's parent")
    rst.add_argument("--workspace", help="Bounded cache directory")
    rst.add_argument("--offline", action="store_true", help="Require the whole pack verified before writing")
    ast = sub.add_parser("archive-status", help="ANIM-013: object inventory and seal state of an archive manifest")
    ast.add_argument("manifest")
    ast.add_argument("--root")
    jstat = sub.add_parser("journal-status", help="ANIM-021: durable journal chain health and the rebuilt job picture for a coordinator state dir")
    jstat.add_argument("state_dir")
    jrec = sub.add_parser("journal-reconcile", help="ANIM-021: rebuild the runtime snapshot from the durable journal and list jobs still needing explicit reconcile")
    jrec.add_argument("state_dir")
    seal_rec = sub.add_parser("archive-seal-reconcile", help="ANIM-021: replay a commit dir's journal and resolve MANIFEST_INTENT/SEAL_UNKNOWN on a local archive root")
    seal_rec.add_argument("commit_dir")
    seal_rec.add_argument("--root", required=True,
                          help="Archive backend root (objects/)")
    for name in ["review-cut", "review-transition"]:
        review = sub.add_parser(name, help="FRAME_ANIMATION_V1: record a human review bound to the current adopted digests")
        review.add_argument("project")
        review.add_argument("target", help="Instance id (I001) or transition id (T001)")
        review.add_argument("--reviewer", required=True)
        review.add_argument("--methods", required=True, help="Comma-separated review methods, e.g. CUT_FULL_SPEED_PLAYBACK")
        review.add_argument("--decision", choices=["APPROVED", "FIX_REQUIRED"], default="APPROVED")
    approve_film = sub.add_parser("approve-film", help="FRAME_ANIMATION_V1: record a FINAL_FILM review bound to a sealed Build 2 manifest and deliverable")
    approve_film.add_argument("project")
    approve_film.add_argument("--build", required=True)
    approve_film.add_argument("--deliverable", default="MASTER_SUBBED.mp4")
    approve_film.add_argument("--reviewer", required=True)
    approve_film.add_argument("--methods", required=True, help="Comma-separated review methods")
    approve_film.add_argument("--decision", choices=["APPROVED", "FIX_REQUIRED"], default="APPROVED")
    review_list = sub.add_parser("animation-reviews", help="Show per-cut/per-transition review state (CURRENT/STALE/UNREVIEWED)")
    review_list.add_argument("project")
    wave_plan = sub.add_parser("animation-waves", help="FRAME_ANIMATION_V1: declare the production wave order (W00 leads) or show it")
    wave_plan.add_argument("project")
    wave_plan.add_argument("--file", help="JSON file holding the ordered waves list")
    locks_show = sub.add_parser("animation-locks", help="FRAME_ANIMATION_V1: show PLAN/WAVE/FINAL scope lock state")
    locks_show.add_argument("project")
    lock_cmd = sub.add_parser("animation-lock", help="FRAME_ANIMATION_V1: record one scope lock (PLAN_LOCK/WAVE_LOCK/FINAL_LOCK)")
    lock_cmd.add_argument("project")
    lock_cmd.add_argument("--scope", choices=["PLAN_LOCK", "WAVE_LOCK", "FINAL_LOCK"], required=True)
    lock_cmd.add_argument("--wave", help="Wave id; required for WAVE_LOCK")
    lock_cmd.add_argument("--approver", required=True)
    route = sub.add_parser("route-decision", help="FRAME_ANIMATION_V1: record the initial wave's KEEP/CHANGE/MIX route decision")
    route.add_argument("project")
    route.add_argument("wave")
    route.add_argument("--decision", choices=["keep", "change", "mix"], required=True)
    route.add_argument("--decider", required=True)
    route.add_argument("--approver", required=True)
    route.add_argument("--apply-scope", default="", help="Comma-separated later wave ids this decision opens")
    route.add_argument("--checked", default="", help="Comma-separated verified difficulty types")
    route.add_argument("--unchecked", default="", help="Comma-separated unverified difficulty types")
    route.add_argument("--conditions", default="", help="Comma-separated reviewed conditions")
    route.add_argument("--observations", default="", help="Comma-separated observation grounds")
    route.add_argument("--changes", default="", help="Comma-separated change items (CHANGE/MIX)")
    route.add_argument("--cost-time-impact", default="")
    route.add_argument("--grounds", default="", help="Recorded grounds; required for an early decision")
    route.add_argument("--early", action="store_true", help="Decide before every wave cut reached adoption")
    w00 = sub.add_parser("w00-status", help="ANIM-022: W00 pilot records, next-wave gate OPEN/BLOCKED with reasons, quote/usage/UNKNOWN and per-deliverable approval state; evidence facets stay UNQUALIFIED/PENDING/NOT_AUTHORIZED")
    w00.add_argument("project")
    packets = sub.add_parser("packets", help="Write per-shot prompts and import commands for making media by hand in subscription apps; no provider calls")
    packets.add_argument("project")
    packets.add_argument("--shots", help="Comma-separated shot IDs; default all shots")
    images = sub.add_parser("images-estimate", help="Quote missing reference images or first frames with the chosen image model; no request is sent")
    images.add_argument("project")
    images.add_argument("--kind", choices=["references", "frames"], default="frames")
    images.add_argument("--shots", help="Comma-separated shot or entry IDs to regenerate; default everything missing")
    approve_images = sub.add_parser("images-approve", help="Approve the exact current image estimate")
    approve_images.add_argument("project")
    approve_images.add_argument("--kind", choices=["references", "frames"], default="frames")
    approve_images.add_argument("--estimate-id", required=True)
    generate_images = sub.add_parser("images-generate", help="Generate the approved images with the chosen image model")
    generate_images.add_argument("project")
    generate_images.add_argument("--kind", choices=["references", "frames"], default="frames")
    sub.add_parser("autopilot", help="Advance a project to its next human decision with the chosen models").add_argument("project")
    listing = sub.add_parser("providers", help="List the models you can choose, with cost and connection state")
    listing.add_argument("--stage", choices=["text", "image", "video"])
    use = sub.add_parser("use", help="Choose the model for one stage of a project")
    use.add_argument("project")
    use.add_argument("--stage", choices=["text", "image", "video"], required=True)
    use.add_argument("--provider", required=True)
    setkey = sub.add_parser("set-key", help="Save an API key for this user (asked without echo; never stored in a project)")
    setkey.add_argument("name", help="e.g. GEMINI_API_KEY")
    setkey.add_argument("--clear", action="store_true")
    direct = sub.add_parser("direct", help="Ask the chosen text model to draft story, cast, locations and shot directions for review")
    direct.add_argument("project")
    direct.add_argument("--notes", default="", help="Extra instructions for the director")
    direct.add_argument("--language", default="English", help="Language of image/video prompt text")
    direct.add_argument("--resume", action="store_true", help="Continue an interrupted draft with the same inputs")
    accept = sub.add_parser("direct-accept", help="Write the reviewed proposal into the production files")
    accept.add_argument("project")
    accept.add_argument("--reviewer", required=True)
    verify = sub.add_parser("verify-build")
    verify.add_argument("build_dir")
    replay = sub.add_parser("replay-build")
    replay.add_argument("build_dir")
    replay.add_argument("--output", required=True)
    sub_reg = sub.add_parser("subscription-register", help="ANIM-016: store a secret-free subscription entitlement record (account binding is a SHA-256 digest, never an email or token)")
    sub_reg.add_argument("--state-dir", required=True, help="Coordinator state dir holding subscriptions/")
    sub_reg.add_argument("--service", required=True)
    sub_reg.add_argument("--usage-path", required=True,
        choices=["CODE_RUNTIME_FILE_PACKET", "HOSTED_NOTEBOOK_UI"])
    sub_reg.add_argument("--account-binding", required=True,
        help="64-hex SHA-256 digest of the account identity — the CLI never accepts a raw account name or credential")
    sub_reg.add_argument("--allowance", default="{}",
        help="JSON map of separate allowance units: subscription_units, compute_units, api_credits, usd, handoff_minutes; integer or UNKNOWN/UNLIMITED/NOT_APPLICABLE")
    sub_reg.add_argument("--inclusion", choices=["CONFIRMED", "UNKNOWN", "NO"], default="UNKNOWN")
    sub_reg.add_argument("--official-execution-api", action="store_true",
        help="Only for a service with a real official execution API; a consumer site is MANUAL")
    sub_reg.add_argument("--service-caps", default="{}",
        help="JSON map of the service's declared file/runtime limits")
    sub_reg.add_argument("--expires-ms", type=int, default=None)
    sub_reg.add_argument("--note", default="")
    sub_probe = sub.add_parser("subscription-probe", help="ANIM-016: probe a subscription runtime into capability evidence; --fake runs the shipped worker script on a fixture and stays FAKE/UNQUALIFIED")
    sub_probe.add_argument("--state-dir", required=True)
    sub_probe.add_argument("--service", required=True)
    sub_probe.add_argument("--fake", action="store_true", help="Run the FAKE subscription runtime fixture — never real-service qualification")
    sub_probe.add_argument("--work-dir", help="Fixture working directory for --fake")
    sub_status = sub.add_parser("subscription-status", help="ANIM-016: entitlement and probe evidence state; real-service qualification is UNQUALIFIED")
    sub_status.add_argument("--state-dir", required=True)
    sub_exp = sub.add_parser("subscription-export", help="ANIM-016: export manual execution packet(s) for a plan's SUBSCRIPTION_CODE_RUNTIME operations")
    sub_exp.add_argument("--state-dir", required=True)
    sub_exp.add_argument("--plan", required=True, help="ExecutionPlan 1 JSON")
    sub_exp.add_argument("--service", required=True)
    sub_exp.add_argument("--out", required=True, help="Output dir for packet dirs")
    sub_exp.add_argument("--job", help="One job key; default every subscription operation")
    sub_exp.add_argument("--input", action="append", default=[],
        help="Input file staged into every packet (repeatable)")
    sub_exp.add_argument("--approve-charges", action="store_true",
        help="Separate explicit approval, required when the plan also sets allow_additional_charges")
    sub_imp = sub.add_parser("subscription-import", help="ANIM-016: import and independently verify a packet result produced by the shipped worker script")
    sub_imp.add_argument("--state-dir", required=True)
    sub_imp.add_argument("--service", required=True)
    sub_imp.add_argument("--packet-dir", required=True)
    sub_imp.add_argument("--result-dir", required=True)
    encoders = sub.add_parser("encoders", help="ANIM-015: probe encoder drivers and report capability evidence; absent hardware is UNAVAILABLE, never fabricated")
    encoders.add_argument("--driver", help="Probe one driver")
    cap_status = sub.add_parser("capability-status", help="ANIM-019: every stored capability evidence entry with its current computed state and machine-readable reasons")
    cap_status.add_argument("--state-dir", required=True, help="State dir holding capability/CE-*.json")
    cap_status.add_argument("--observation", help="JSON file with a fresh environment/entitlement observation for staleness checks")
    cap_status.add_argument("--now-ms", type=int, default=None)
    cap_pre = sub.add_parser("capability-preflight", help="ANIM-019: filter a plan's route/worker/encoder candidates to currently eligible ones; AUTO_PERFORMANCE never selects without a bound measurement")
    cap_pre.add_argument("target", help="Project dir holding execution/plan.json, or an ExecutionPlan 1 JSON file")
    cap_pre.add_argument("--state-dir", required=True)
    cap_pre.add_argument("--observation", help="JSON file with a fresh environment/entitlement observation")
    cap_pre.add_argument("--now-ms", type=int, default=None)
    perf_plan = sub.add_parser("perf-plan", help="ANIM-017: dependency graph + AUTO_PERFORMANCE ExecutionPlan + capability preflight; nothing executes")
    perf_plan.add_argument("project")
    perf_plan.add_argument("--state-dir", help="Capability registry state dir; default <project>/render/perf/registry")
    forecast = sub.add_parser("resource-forecast", help="Node film-resource-forecast: pre-execution resource forecast for an ExecutionPlan — per-line measurement provenance or UNKNOWN, disk/entitlement/quote checks, proposed alternatives; nothing executes or charges")
    forecast.add_argument("project")
    forecast.add_argument("--plan", help="ExecutionPlan 1 JSON; default <project>/execution/plan.json then the perf graph's AUTO_PERFORMANCE plan")
    forecast.add_argument("--state-dir", help="Registry + subscriptions dir; default <project>/render/perf/registry")
    forecast.add_argument("--observation", help="JSON file with a full current observation (environment plus driver/credential_epoch/allowance/caps/route/transport bindings). The default observation records only the device environment, not those bindings, so stored evidence stays STALE under it; a full observation file is needed to make evidence CURRENT")
    forecast.add_argument("--no-observe", action="store_true",
        help="Skip the current observation — stored evidence stays STALE and every measured line degrades to UNKNOWN")
    forecast.add_argument("--disk-free-bytes", type=int, default=None,
        help="Override the observed USER_DESKTOP free-space observation")
    forecast.add_argument("--packet", action="append", default=[],
        help="Issued packet dir (or packet.json); a quote change since issuance is reported")
    forecast.add_argument("--now-ms", type=int, default=None)
    perf_bench = sub.add_parser("perf-benchmark", help="ANIM-017: cold/warm/one-cut/interrupt benchmark on a local working copy; secret-free report; complete bound measurements register into the perf registry")
    perf_bench.add_argument("project")
    perf_bench.add_argument("--work-dir", help="Working copy dir (default: system temp)")
    perf_bench.add_argument("--runs", type=int, default=1, help="Cold+warm sample repetitions")
    perf_bench.add_argument("--workers", type=int, default=1, help="Bounded parallel compose cap")
    perf_bench.add_argument("--prefetch", type=int, default=0, help="Upcoming dirty frames to prefetch members for")
    perf_bench.add_argument("--pack", action="store_true", help="Also run PACK-SPARSE phases through the fake archive backend")
    perf_bench.add_argument("--no-register", action="store_true", help="Skip registering evidence/measurements into <project>/render/perf/registry")
    prov = sub.add_parser("provenance-report", help="ANIM-020: canonical render_manifest + dependency graph + stale-approval projection for the committed render state")
    prov.add_argument("project")
    prov.add_argument("--perf-root", help="Perf state dir holding a committed state.json (default <project>/render/perf)")
    reb = sub.add_parser("rebuild-plan", help="ANIM-020: change-closure plan — read/member/halo scope, compose/subbed/encode ranges and the stale-approval projection for a declared change set")
    reb.add_argument("project")
    reb.add_argument("--changes", required=True,
                     help="JSON file with the declared change list, e.g. "
                          "[{\"class\": \"PICTURE\", \"instance_id\": \"I002\", \"member_index\": 7}]")
    encode_build = sub.add_parser("encode-build", help="ANIM-015: encode+verify a sealed Build 2 build's delivery frames with a chosen driver")
    encode_build.add_argument("build_dir")
    encode_build.add_argument("--driver", required=True,
        choices=["FFMPEG", "GSTREAMER", "NVIDIA_NATIVE",
                 "VIDEOTOOLBOX_NATIVE", "QUALIFIED_SERVICE"])
    encode_build.add_argument("--output", required=True)
    encode_build.add_argument("--import-packets", dest="import_path",
        help="QUALIFIED_SERVICE: packet file returned by the external encode")
    for name in ["analyze", "package", "lock", "estimate", "approve", "compile", "import-frame", "economy-init", "work-status", "edit-take", "claim-job"]:
        p = sub.add_parser(name, help="Legacy shot generation/resume (may call paid renderers); use compile-preview for a local full-song build" if name == "compile" else None)
        p.add_argument("project")
        if name in {"estimate", "compile"}:
            p.add_argument("--seconds", type=float)
            p.add_argument("--renderer", choices=["mock", "manual", "openart", "fal", "economy", "gemini"], default="mock")
            p.add_argument("--quality", choices=["draft", "final"], default="final")
        if name == "compile":
            p.add_argument("--output")
        if name == "package":
            p.add_argument("--preset", choices=["neutral", "water_please"], help="Optional production preset; new projects default to neutral")
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
        elif a.command == "animation-init":
            from .animation_migrate import animation_init
            result = animation_init(a.project, profile=a.profile)
        elif a.command == "import-sequence":
            from .animation_assets import import_frame_sequence
            result = import_frame_sequence(a.project, a.shot, index=a.index,
                                           folder=a.folder, asset_id=a.asset_id,
                                           note=a.note)
        elif a.command == "import-animation-asset":
            from .animation_assets import (import_layer_rgba, import_mask,
                                           import_replacement_drawing,
                                           import_rig_spec)
            def _pin_arg(value, flag):
                if not value:
                    raise FilmError(f"--{flag} is required as "
                                    "asset_id:revision:sha256")
                parts = value.split(":")
                if len(parts) != 3:
                    raise FilmError(f"--{flag} must be "
                                    "asset_id:revision:sha256")
                return {"asset_id": parts[0], "revision": int(parts[1]),
                        "content_sha256": parts[2]}
            kind = a.kind.upper()
            pivot = [int(v) for v in a.pivot.split(",")]
            if kind == "LAYER_RGBA":
                origin = [int(v) for v in a.crop_origin.split(",")]
                result = import_layer_rgba(a.project, a.file,
                                           asset_id=a.asset_id,
                                           pivot=pivot, crop_origin=origin,
                                           z_order=a.z_order, note=a.note)
            elif kind == "MASK":
                region = ([int(v) for v in a.region.split(",")]
                          if a.region else None)
                result = import_mask(a.project, a.file,
                                     target=_pin_arg(a.target, "target"),
                                     region=region, channel=a.channel,
                                     asset_id=a.asset_id, note=a.note)
            elif kind == "REPLACEMENT_DRAWING":
                if a.frame is None:
                    raise FilmError("--frame is required for "
                                    "REPLACEMENT_DRAWING")
                result = import_replacement_drawing(
                    a.project, a.file,
                    replaces=_pin_arg(a.replaces, "replaces"),
                    frame=a.frame, pivot=pivot, asset_id=a.asset_id,
                    note=a.note)
            elif kind == "RIG_SPEC":
                spec = json.loads(Path(a.file).read_text(encoding="utf-8"))
                result = import_rig_spec(a.project, spec,
                                         asset_id=a.asset_id, note=a.note)
            else:
                raise FilmError(f"Asset kind {a.kind} is not implemented "
                                "in this node")
        elif a.command == "shot-plan":
            from .motion_plan import save_shot_plan
            document = json.loads(Path(a.file).read_text(encoding="utf-8"))
            result = save_shot_plan(a.project, document)
        elif a.command == "compile-shot":
            from .compositor import composite_shot
            result = composite_shot(a.project, a.shot,
                                    instance_id=a.instance)
        elif a.command == "animation-assets":
            from .animation_assets import asset_status
            result = asset_status(a.project)
        elif a.command == "animation-validate":
            from .frame_sequence import animation_validate
            result = animation_validate(a.project)
        elif a.command == "animation-packets":
            from .packets import export_work_packets
            target = json.loads(Path(a.target).read_text(encoding="utf-8")) \
                if a.target else None
            result = export_work_packets(
                a.project, a.shots.split(",") if a.shots else None,
                wave=a.wave, target=target)
        elif a.command == "import-control":
            from .animation_assets import import_draft_image

            def _draft_pin(value):
                parts = value.strip().split(":")
                if len(parts) != 3:
                    raise FilmError("--references entries must be "
                                    "asset_id:revision:sha256")
                return {"asset_id": parts[0], "revision": int(parts[1]),
                        "content_sha256": parts[2]}
            result = import_draft_image(
                a.project, a.shot, a.file, role=a.role, frame=a.frame,
                references=[_draft_pin(v) for v in a.references.split(",")
                            if v.strip()],
                asset_id=a.asset_id, note=a.note)
        elif a.command == "commit-draft-frames":
            from .animation_assets import commit_draft_frames
            result = commit_draft_frames(a.project, a.shot,
                                         asset_id=a.asset_id, note=a.note)
        elif a.command == "segment-quote":
            from .segment_gen import segment_quote
            result = segment_quote(a.project, a.shot, a.start, a.end,
                                   adapter_id=a.adapter)
        elif a.command == "segment-submit":
            from .segment_gen import segment_submit
            result = segment_submit(a.project, a.shot, a.start, a.end,
                                    adapter_id=a.adapter,
                                    approver=a.approver,
                                    quote_id=a.quote_id, cap=a.cap,
                                    max_retries=a.max_retries)
        elif a.command == "segment-reconcile":
            from .segment_gen import segment_reconcile
            result = segment_reconcile(a.project, a.job, adapter_id=a.adapter)
        elif a.command == "segment-cancel":
            from .segment_gen import segment_cancel
            result = segment_cancel(a.project, a.job, adapter_id=a.adapter)
        elif a.command == "segment-import":
            from .segment_gen import segment_import
            result = segment_import(a.project, a.job, adapter_id=a.adapter,
                                    source_start=a.source_start,
                                    source_count=a.source_count)
        elif a.command == "segment-commit":
            from .segment_gen import commit_segment_sequence
            result = commit_segment_sequence(a.project, a.shot,
                                             asset_id=a.asset_id,
                                             note=a.note)
        elif a.command == "segment-jobs":
            from .segment_gen import segment_jobs
            result = segment_jobs(a.project, a.shot)
        elif a.command == "segment-control":
            from .segment_gen import import_segment_control

            def _ctrl_pin(value):
                parts = value.strip().split(":")
                if len(parts) != 3:
                    raise FilmError("--references entries must be "
                                    "asset_id:revision:sha256")
                return {"asset_id": parts[0], "revision": int(parts[1]),
                        "content_sha256": parts[2]}
            result = import_segment_control(
                a.project, a.shot, a.file, role=a.role, frame=a.frame,
                references=[_ctrl_pin(v) for v in a.references.split(",")
                            if v.strip()],
                asset_id=a.asset_id, note=a.note)
        elif a.command == "segment-fake":
            from .segment_fake import configure_fake, fake_state
            if a.fake_set:
                result = configure_fake(
                    a.project,
                    **json.loads(a.fake_set))
            else:
                result = fake_state(a.project)
        elif a.command == "archive-create":
            from .archive_cli import archive_create
            result = archive_create(a.source, a.root, profile=a.profile,
                                    min_level=a.min_level)
        elif a.command == "archive-restore":
            from .archive_cli import archive_restore
            result = archive_restore(a.manifest, a.dest, root=a.root,
                                     workspace=a.workspace,
                                     offline=a.offline)
        elif a.command == "archive-status":
            from .archive_cli import archive_status_cli
            result = archive_status_cli(a.manifest, root=a.root)
        elif a.command == "journal-status":
            from .journal_cli import journal_status
            result = journal_status(a.state_dir)
        elif a.command == "journal-reconcile":
            from .journal_cli import journal_reconcile
            result = journal_reconcile(a.state_dir)
        elif a.command == "archive-seal-reconcile":
            from .journal_cli import archive_seal_reconcile
            result = archive_seal_reconcile(a.commit_dir, a.root)
        elif a.command == "review-cut":
            from .animation_review import record_cut_review
            with project_mutex(a.project):
                result = record_cut_review(a.project, a.target, reviewer=a.reviewer,
                                           methods=a.methods.split(","), decision=a.decision)
        elif a.command == "review-transition":
            from .animation_review import record_transition_review
            with project_mutex(a.project):
                result = record_transition_review(a.project, a.target, reviewer=a.reviewer,
                                                  methods=a.methods.split(","), decision=a.decision)
        elif a.command == "approve-film":
            from .animation_review import record_film_review
            with project_mutex(a.project):
                result = record_film_review(a.project, a.build, reviewer=a.reviewer,
                                            methods=a.methods.split(","),
                                            deliverable=a.deliverable, decision=a.decision)
        elif a.command == "animation-reviews":
            from .animation_review import review_status
            result = review_status(a.project)
        elif a.command == "animation-waves":
            from .animation_locks import declare_waves, load_waves
            if a.file:
                data = json.loads(Path(a.file).read_text(encoding="utf-8"))
                result = declare_waves(a.project,
                                       data["waves"] if isinstance(data, dict) else data)
            else:
                result = load_waves(Path(a.project)) or {"waves": [], "note": f"no production/waves.json"}
        elif a.command == "animation-locks":
            from .animation_locks import lock_status
            result = lock_status(a.project)
        elif a.command == "animation-lock":
            from .animation_locks import (record_final_lock, record_plan_lock,
                                          record_wave_lock)
            if a.scope == "PLAN_LOCK":
                result = record_plan_lock(a.project, a.approver)
            elif a.scope == "FINAL_LOCK":
                result = record_final_lock(a.project, a.approver)
            else:
                if not a.wave:
                    raise FilmError("--wave is required for WAVE_LOCK")
                result = record_wave_lock(a.project, a.wave, a.approver)
        elif a.command == "route-decision":
            from .animation_locks import record_route_decision

            def _csv(value):
                return [s.strip() for s in value.split(",") if s.strip()]

            result = record_route_decision(
                a.project, a.wave, decision=a.decision.upper(),
                decider=a.decider, approver=a.approver,
                checked_types=_csv(a.checked), unchecked_types=_csv(a.unchecked),
                apply_scope=_csv(a.apply_scope),
                reviewed_conditions=_csv(a.conditions),
                observations=_csv(a.observations), changes=_csv(a.changes),
                cost_time_impact=a.cost_time_impact,
                early=a.early, grounds=a.grounds)
        elif a.command == "w00-status":
            from .w00_gate import w00_status
            result = w00_status(a.project)
        elif a.command == "builds":
            from .builds import list_builds
            result = list_builds(a.project)
        elif a.command == "packets":
            from .packets import export_packets
            result = export_packets(a.project, a.shots.split(",") if a.shots else None)
        elif a.command == "images-estimate":
            from . import imagegen, providers
            result = imagegen.estimate(a.project, a.kind, providers.selected_image(a.project), a.shots.split(",") if a.shots else None)
        elif a.command == "images-approve":
            from . import imagegen
            result = imagegen.approve(a.project, a.kind, a.estimate_id)
        elif a.command == "images-generate":
            from . import imagegen, providers
            result = imagegen.generate(a.project, a.kind, providers.selected_image(a.project))
        elif a.command == "autopilot":
            from .autopilot import autopilot
            result = autopilot(a.project)
        elif a.command == "providers":
            from . import providers
            result = [{"id": x.id, "stage": x.stage, "name": x.label, "cost": providers.COSTS[x.cost],
                       **{k: v for k, v in providers.status(x.id).items() if k != "keys"}}
                      for x in (providers.providers_for(a.stage) if a.stage else providers.PROVIDERS.values())]
        elif a.command == "use":
            from . import providers
            result = providers.select(a.project, a.stage, a.provider)
        elif a.command == "set-key":
            import getpass
            from .settings import delete_secret, set_secret
            if a.clear:
                delete_secret(a.name)
                result = {"cleared": a.name}
            else:
                set_secret(a.name, getpass.getpass(f"{a.name}: "))
                result = {"saved": a.name}
        elif a.command == "direct":
            from . import director, providers
            provider = providers.selected_text(a.project)
            proposal = director.draft(a.project, provider, a.notes, a.language, a.resume,
                                      progress=lambda message: print(message, file=sys.stderr, flush=True))
            result = {"shots": len(proposal["shots"]), "characters": [c["id"] for c in proposal["world"]["characters"]],
                      "locations": [x["id"] for x in proposal["world"]["locations"]], "warnings": proposal["warnings"],
                      "next": "제안을 확인한 뒤: direct-accept --reviewer <이름> (제안 파일: bible/director_proposal.json)"}
        elif a.command == "direct-accept":
            from . import director
            result = director.accept(a.project, a.reviewer)
        elif a.command == "verify-build":
            from .builds import verify_build
            result = verify_build(a.build_dir)
        elif a.command == "replay-build":
            from .builds import replay_build
            result = replay_build(a.build_dir, a.output)
        elif a.command == "subscription-register":
            from .execution_workers import subscription as sub_mod
            doc = sub_mod.make_entitlement(
                a.service, usage_path=a.usage_path,
                account_binding=a.account_binding.lower(),
                official_execution_api=a.official_execution_api,
                allowance=json.loads(a.allowance),
                inclusion={"execution_in_subscription": a.inclusion,
                           "note": a.note or
                           ("inclusion declared " + a.inclusion)},
                service_caps=json.loads(a.service_caps),
                expires_at_ms=a.expires_ms, notes=a.note)
            path = sub_mod.save_entitlement(a.state_dir, doc)
            result = {"entitlement": str(path),
                      "entitlement_id": doc["entitlement_id"],
                      "route_label": sub_mod.route_label(doc),
                      "qualification_state": "UNQUALIFIED"}
        elif a.command == "subscription-probe":
            from .animation_schema import read_canon as _read_canon
            from .execution_workers import subscription as sub_mod
            entitlement = sub_mod.load_entitlement(a.state_dir, a.service)
            runtime = None
            if a.fake:
                runtime = sub_mod.FakeSubscriptionRuntime(entitlement)
            evidence = sub_mod.probe_subscription(
                a.service, entitlement=entitlement, runtime=runtime,
                work_dir=a.work_dir)
            path = sub_mod.save_evidence(a.state_dir, evidence)
            result = {"evidence": str(path),
                      "evidence_id": evidence["evidence_id"],
                      "registry_state": evidence["registry_state"],
                      "qualification": evidence["qualification"],
                      "fake": bool(a.fake)}
        elif a.command == "subscription-status":
            from .execution_workers import subscription as sub_mod
            folder = sub_mod.subscriptions_dir(a.state_dir)
            entitlements = [sub_mod.validate_entitlement(
                json.loads(p.read_text("utf-8")))
                for p in sorted(folder.glob("*.json"))] \
                if folder.is_dir() else []
            result = {
                "entitlements": [
                    {"service": e["service"],
                     "entitlement_id": e["entitlement_id"],
                     "credential_epoch": e["credential_epoch"],
                     "route_label": sub_mod.route_label(e),
                     "allowance": e["allowance"], "used": e["used"],
                     "inclusion": e["inclusion"]}
                    for e in entitlements],
                "evidence": [
                    {"evidence_id": ev["evidence_id"],
                     "registry_state": ev["registry_state"],
                     "qualification": ev["qualification"],
                     "observed_at": ev["observed_at"]}
                    for ev in sub_mod.list_evidence(a.state_dir)],
                "facets": sub_mod.subscription_facet_report()}
        elif a.command == "subscription-export":
            from .execution_packets import export_manual_packets
            from .execution_plan import job_key, validate_execution_plan
            from .animation_schema import read_canon as _read_canon
            from .execution_workers import Coordinator
            from .execution_workers import subscription as sub_mod
            plan = validate_execution_plan(_read_canon(a.plan))
            entitlement = sub_mod.load_entitlement(a.state_dir, a.service)
            worker = sub_mod.SubscriptionWorker(entitlement)
            coordinator = Coordinator(a.state_dir)
            ops = [o for o in plan["operations"]
                   if o["route"] == "SUBSCRIPTION_CODE_RUNTIME"]
            if not ops:
                raise FilmError("plan has no SUBSCRIPTION_CODE_RUNTIME "
                                "operations")
            keys = [job_key(plan, o) for o in ops]
            if a.job:
                keys = [k for k in keys if k == a.job]
                if not keys:
                    raise FilmError(f"job {a.job} is not a subscription "
                                    "operation of this plan")
            if not all(k in coordinator.jobs for k in keys):
                coordinator.plan_jobs(plan)
            exported = []
            for key in keys:
                if coordinator.jobs[key]["state"] == "PLANNED":
                    coordinator.reserve(key)
                exported.append(export_manual_packets(
                    coordinator, plan, key,
                    sub_mod.entitlement_path(a.state_dir, a.service),
                    worker, a.out, input_files=a.input,
                    additional_charges_approved=a.approve_charges))
            result = {"service": a.service,
                      "route_label": sub_mod.route_label(entitlement,
                                                         worker),
                      "jobs": exported,
                      "qualification_state": "UNQUALIFIED"}
        elif a.command == "subscription-import":
            from .execution_packets import import_result
            from .execution_workers import Coordinator
            from .execution_workers import subscription as sub_mod
            entitlement = sub_mod.load_entitlement(a.state_dir, a.service)
            worker = sub_mod.SubscriptionWorker(entitlement)
            coordinator = Coordinator(a.state_dir)
            outcome = import_result(coordinator, worker, a.packet_dir,
                                    a.result_dir, entitlement=entitlement)
            if coordinator.jobs[outcome["job_key"]]["state"] == \
                    "OUTPUT_PENDING_VERIFY":
                outcome["verified"] = coordinator.verify_outputs(
                    outcome["job_key"])
            outcome["qualification_state"] = "UNQUALIFIED"
            result = outcome
        elif a.command == "encoders":
            from .encoder_backends import probe_all, probe_driver
            result = probe_driver(a.driver) if a.driver else probe_all()
        elif a.command == "capability-status":
            from .capability_registry import registry_status
            observation = json.loads(Path(a.observation)
                                     .read_text(encoding="utf-8")) \
                if a.observation else None
            result = registry_status(a.state_dir,
                                     observation=observation,
                                     now_ms=a.now_ms)
        elif a.command == "capability-preflight":
            from .animation_schema import read_canon as _read_plan
            from .capability_registry import plan_preflight
            from .execution_plan import validate_execution_plan
            target = Path(a.target)
            plan_path = target / "execution" / "plan.json" \
                if target.is_dir() else target
            if not plan_path.is_file():
                raise FilmError(f"no ExecutionPlan 1 at {plan_path}")
            plan = validate_execution_plan(_read_plan(plan_path))
            observation = json.loads(Path(a.observation)
                                     .read_text(encoding="utf-8")) \
                if a.observation else None
            result = plan_preflight(plan, state_dir=a.state_dir,
                                    observation=observation,
                                    now_ms=a.now_ms)
        elif a.command == "perf-plan":
            from .perf_scheduler import perf_plan_command
            result = perf_plan_command(a.project, state_dir=a.state_dir)
        elif a.command == "resource-forecast":
            from .resource_forecast import resource_forecast_command
            observation = json.loads(Path(a.observation)
                                     .read_text(encoding="utf-8")) \
                if a.observation else None
            disk = {"USER_DESKTOP": {"free_bytes": a.disk_free_bytes,
                                     "source": "--disk-free-bytes"}} \
                if a.disk_free_bytes is not None else None
            result = resource_forecast_command(
                a.project, plan_path=a.plan, state_dir=a.state_dir,
                observation=observation, observe=not a.no_observe,
                disk=disk, packet_paths=a.packet, now_ms=a.now_ms)
        elif a.command == "perf-benchmark":
            from .perf_scheduler import perf_benchmark_command
            result = perf_benchmark_command(
                a.project, work_root=a.work_dir, runs=a.runs,
                workers=a.workers, prefetch=a.prefetch,
                use_packs=a.pack, register=not a.no_register)
        elif a.command == "provenance-report":
            from .render_provenance import provenance_report
            result = provenance_report(a.project, perf_root=a.perf_root)
        elif a.command == "rebuild-plan":
            from .render_provenance import rebuild_plan_report
            changes = json.loads(Path(a.changes).read_text(encoding="utf-8"))
            result = rebuild_plan_report(a.project, changes)
        elif a.command == "encode-build":
            from .animation_compiler import encode_build_delivery
            result = encode_build_delivery(a.build_dir, a.driver, a.output,
                                           import_path=a.import_path)
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
            result = str(init_project(a.root, a.name, a.audio, Path(a.brief).read_text(encoding="utf-8"), Path(a.lyrics).read_text(encoding="utf-8") if a.lyrics else "", aspect=a.aspect))
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
