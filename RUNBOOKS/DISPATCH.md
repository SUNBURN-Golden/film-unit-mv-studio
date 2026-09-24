# Shared engineering control plane

Shared source owner: `BeautifulMind-JT/ai-ops-control-plane`. This product is not the shared control-plane host.

Migration decision: https://github.com/BeautifulMind-JT/ai-ops-control-plane/issues/1

Imported source candidate: https://github.com/BeautifulMind-JT/ai-ops-control-plane/tree/d8b096994f9e8510fafd00a35f9ea5e4b33925c6/engineering
Target product: `BeautifulMind-JT/film-unit-mv-studio`. Product contracts, tasks, locked files and product CI stay here.

This is source/reference extraction, NOT production cutover. The destination import must
be independently reviewed and merged first. Do not enable a runner, copy credentials,
start a builder or assume KIX activation/audit evidence transfers. Preserve task, owner,
request and ledger identities. Fence/drain legacy dispatch before retiring it; never run
two dispatchers. Runtime identity separation and host/Slack cutover are separate gates.

User-only merge; no automatic fallback, retries, polling or standing routines.
