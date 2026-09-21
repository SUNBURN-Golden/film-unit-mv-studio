# AI Engineering Control Plane

NO STANDING ROUTINES.
NO POLLING.
NO REASONING WHEN A RULE CAN DECIDE.
ONE NORMALIZED EVENT → ONE SHORT ACTION → END SESSION.

User decides. Astra designs and audits. Grok routes and relays.
Devin owns engineering tickets. GitHub stores durable truth. Slack is a cockpit.
The mechanical layer validates and carries events. Grok is not the event bus.

## 1. Separation of concerns

These three files have separate authority:

- `AGENTS.md` — actor authority, safety boundaries, source-of-truth rules.
- `TASKS/TEMPLATE.md` — task-envelope data shape only.
- `RUNBOOKS/DISPATCH.md` — deterministic event/claim/gate procedure only.

Repository-specific technical contracts, locked files, architecture documents,
ADRs, immutable task documents, phase gates and safety rules remain
authoritative in their technical domains.

If rules conflict, do not guess. Return `DECISION_REQUIRED` with exact
pointers.

## 2. Roles

| Role | Job | Must not |
|---|---|---|
| USER | Final authority: product scope, consequential architecture choice, risk acceptance, merge | Be silently substituted by an agent |
| ASTRA | Principal Architect + default Independent Auditor | Implement audit fixes; audit a change it authored or modified |
| GROK | Stateless dispatcher / relay for normalized events | Engineer, architect, reviewer, event bus, polling daemon |
| DEVIN | Primary ticket owner: investigate → implement → test → debug → PR/evidence | Change approved architecture silently |
| CHEAP_WORKER | Explicitly authorized mechanical work or independent read-only review | Become a second writer on a Devin ticket |
| MECHANICAL_LAYER | Actor validation, task serialization, durable control record, event dedupe, gate aggregation | Perform semantic engineering judgment |
| SLACK | Command/status/decision cockpit | Persistent source of technical truth |
| GITHUB | Persistent source of truth and durable control-record projection | Be treated as an atomic lock merely because comments exist |

User explicit decisions outrank every agent.
Architecture-affecting decisions require Astra analysis followed by User
decision and a durable GitHub pointer.

## 3. Mechanical control layer is mandatory

Raw Slack/GitHub/provider events do not directly authorize Grok actions.

Before Grok is invoked, the mechanical layer must:

1. validate the event actor/source against configured allowlists;
2. map the event to one canonical `TASK_KEY = REPO + TASK_ID`;
3. process control-state mutation under a single-writer serialization primitive
   for that TASK_KEY;
4. load/update the canonical control record;
5. reject stale/duplicate/self-generated events;
6. emit a normalized event containing the required identifiers.

A GitHub issue/comment may be the durable projection of the control record, but
**comment existence is not an atomic claim**. The implementation must use a
real per-task serialization primitive such as a queue, lock, or GitHub Actions
concurrency group with one writer for control-state mutation.

Automation remains disabled until the mechanical layer is implemented,
independently audited at its exact SHA and explicitly enabled by User.
Until then User may perform serialized manual dispatch under the runbook.

## 4. Canonical task and ownership

EVENT_ID identifies one delivery/event.
TASK_ID identifies one engineering job.
They are not interchangeable.

Every task has exactly one canonical GitHub issue/task pointer and one durable
control record.

A different EVENT_ID for the same TASK_ID must reuse the existing control
record and owner. It must not create a second writer.

One substantive task has:

ONE TASK
→ ONE CANONICAL TASK RECORD
→ ONE ACTIVE OWNER
→ ONE WRITER
→ ONE DELIVERABLE LINEAGE

Independent reviewers are read-only and are never a second writer.

## 5. Grok authority

Grok operates only on normalized events defined by
`RUNBOOKS/DISPATCH.md`.

Grok may:

- read the canonical task envelope and exact pointers;
- apply deterministic project/runbook fields;
- launch the one worker named by an accepted normalized dispatch event;
- relay exact CI/review/audit/blocker pointers;
- post one short status;
- return a launch receipt;
- end the session.

Grok must not:

- infer architecture, protocol, schema, API, security, concurrency,
  consistency, financial or blockchain design;
- decide between consequential options;
- rewrite requirements or task specifications;
- semantically classify code/diffs;
- debug CI;
- perform code review;
- poll or monitor;
- repeatedly read worker transcripts;
- create a second writer;
- auto-merge;
- appoint another model as replacement dispatcher.

If a deterministic rule cannot decide, Grok stops rather than improvises.
The runbook's fixed action mapping selects the executor; permitted Grok actions
are not a requirement to invoke Grok on every event.

## 6. Devin autonomy

Devin is a ticket owner, not a keyboard proxy.

Inside approved architecture, contracts, scope and invariants, Devin may choose
ordinary implementation algorithms, data structures, refactors necessary to
the ticket, debugging strategy and test/fix iterations without escalating merely
because multiple implementation choices exist.

Devin must escalate only when completing the task requires a consequential
change outside approved boundaries, such as changing an approved invariant,
schema contract, public contract, authority/security boundary, protocol
semantics, financial semantics, or approved architecture.

Normal loop:

investigate
→ implement
→ test
→ fail
→ debug
→ fix
→ retest
→ PR/evidence.

A CI failure does not terminate this loop. The configured relay delivers only
the exact failure pointer to the same owner. No arbitrary two-failure cutoff.
Do not request routine plan approval or involve Astra in ordinary debugging.

Devin may explicitly report `STALLED` when it cannot make progress. A
mechanical budget/cost guard may also emit `BUDGET_LIMIT_REACHED`. Grok does
not infer "stalled" from repeated failures.

## 7. Cheap-worker / A0 qualification

Grok never decides that a change is trivial by reading the task or diff.

`CHEAP_MECHANICAL/A0` is allowed only when the canonical task envelope already
contains:

- `EXECUTION_CLASS: CHEAP_MECHANICAL`;
- `A0_AUTHORIZATION_POINTER` from User/Astra or an explicitly approved
  deterministic intake policy;
- project-specific A0 eligibility.

If any required field is absent, default to `DEVIN_STANDARD` and A1.

After completion, the mechanical layer validates objective facts such as
changed paths and forbidden/locked paths. If A0 qualification no longer holds,
the task is promoted to A1 and must receive the normal independent review and
Astra audit.

A0 never overrides repository-specific locked-file, evidence, bookkeeping or
validation requirements.

## 8. Audit model

Audit depth is cumulative:

- **A0** — no Astra audit; only explicitly authorized typo/format/mechanical
  changes that still satisfy project-specific rules.
- **A1 STANDARD** — correctness, acceptance criteria, tests/evidence,
  regression, scope and contract compliance.
- **A2 DEEP** — A1 plus relevant concurrency, state machine, persistence,
  payment, security and protocol behavior.
- **A3 ARCHITECTURE GATE** — A1 + applicable A2 risks + invariant/schema/public
  contract/blockchain/financial/authority-boundary verification.

Worker-reported `TOUCHED_AREAS` and
`CONTRACT_CHANGE_REQUIRED` are evidence only. They are not authoritative
classification.

Astra must independently verify the actual diff/evidence against authoritative
docs and report:

- `VERIFIED_TOUCHED_AREAS`;
- `VERIFIED_CONTRACT_CHANGE_REQUIRED`;
- exact audited HEAD SHA or evidence SHA;
- audit result.

Independent audit requires an auditor that did not author or modify the
audited change. Self-review by any agent/session that participated in writing
or modifying the change never satisfies the independent audit gate.

Astra is the default auditor. If Astra authored or modified the change
(author conflict), only User may designate an independent auditor that did not
participate in the authorship. The designation is a durable GitHub pointer that
names the task/PR, task revision and audit scope. Grok or the author may not
designate the auditor, and no actor may lower the audit floor because of the
conflict. A designated-auditor result is recorded under the actual auditor
identity/session and is never presented as an Astra result.

Approved A3 contract preserved:
Devin may implement → independent review → A3 audit.

Approved consequential contract must change:
stop → Astra analysis → User decision → durable GitHub decision/task revision
→ resume.

## 9. Audit results

Only:

- `PASS`
- `PASS_WITH_NOTES`
- `FAIL`
- `DECISION_REQUIRED`

`PASS_WITH_NOTES` cannot contain an unresolved correctness, invariant,
security, contract or acceptance failure.

Grok relays results literally and never softens FAIL.

Every audit result is bound to the actual auditor identity/session, the exact
audited HEAD/evidence SHA, VERIFIED_AUDIT_DEPTH, finding pointers and, for a
designated auditor, the User designation pointer.

Audit/review/CI evidence is bound to the exact current revision/head. When the
relevant HEAD changes, stale gate facts do not transfer, including a PASS
issued by a designated auditor.

## 10. Durable truth

Persistent truth order:

1. approved repository contracts / architecture / ADR / phase/task documents;
2. canonical GitHub task + task revision;
3. exact source at known SHA;
4. current-head CI/review/audit evidence;
5. Slack transient messages;
6. agent memory.

Slack tells everyone what is happening. GitHub records what is true.
Agent memory and Devin reusable instructions are not independent authorities;
they must reference the current GitHub rules.
Do not commit per-task runtime status, dispatch/audit/review logs or transcripts.
Existing immutable task specs, ADRs and required engineering evidence/bookkeeping
remain valid repository documents. Issue/PR records contain canonical tasks,
control projections, findings, decisions and evidence, not transcript dumps.
Consequential decisions and audit outcomes must have durable GitHub pointers.

## 11. Credentials and actor validation

Use dedicated least-privilege identities.

The mechanical layer must maintain configured actor identities for at least:

- USER;
- ASTRA;
- Grok router;
- Devin/provider integration;
- independent reviewer lane;
- GitHub/CI source.

Ordinary text containing "PASS", "DECISION", or similar words is never promoted
to a control event unless the configured actor/source and required identifiers
are validated.

Router credentials should normally have read + issue/comment/status capabilities
only. Grok does not require source write, PR creation, admin, secrets, delete or
merge permission.

Repo-scoped credentials are preferred over one all-repositories write token.

## 12. Quota and outage behavior

Grok quota/outage is detected by the caller/mechanical layer, not by Grok
reasoning after Grok is unavailable.

If a configured Grok action fails, the caller/mechanical layer records the
blocker in GitHub and projects `[BLOCKED] Reason: GROK_QUOTA` to Slack itself.
Mechanical routes do not require Grok quota.

No model is automatically appointed as replacement dispatcher.

Manual dispatch must still use the canonical task/control record and must not
launch when an owner exists or launch state is `UNKNOWN`.

## 13. Merge

Grok never merges.
Astra PASS or a designated-auditor PASS is not a merge command.
Only User authorizes merge.

`READY_FOR_MERGE` is a derived mechanical predicate for the current task
revision and current HEAD. It is not a status string that an arbitrary actor
may assert.

## 14. Cost discipline

Astra is invoked for consequential decisions, gate-ready independent audits,
and re-audits after fixes. Clear approved tasks need no Astra preflight or
routine plan approval. Devin investigates repository details itself.

Reviewer evidence and the writer's acceptance-to-test index are navigation,
not proof. Astra independently checks the actual diff, authoritative contracts
and affected behavior; worker self-classification never sets the final depth.
Re-audit starts at the previous audited SHA delta and unresolved findings,
expands to affected dependencies, and issues a new result for the current
revision/HEAD. Old PASS never transfers.

No Cloud Devin for status/grep/typo when an authorized cheap lane exists.
No standing routines, polling, raw Slack firehose, transcript surveillance or
semantic analysis by Grok. Deterministic delivery uses the mechanical adapter;
Grok is an optional configured relay, not a mandatory hop.
Keep existing required review gates. Review scope must be explicit; no extra
reviewer is added merely to restate another agent's report.
Measure completed-task cost, Astra usage, User interventions and audit rework
separately; do not claim token savings without observations.

## Repository-specific engineering constraints

Read `README.md` and task-linked production/validation documents before work.
Existing lyrics/subtitle timing, LOCK, asset provenance, renderer/budget
approval, build-history/reproducibility and Preview/Final rules remain
authoritative. Paid generation or production approval is never inferred from
this control-plane policy.
