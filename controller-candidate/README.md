# Deterministic native-collaboration MVP candidate

**Status:** deterministic-only integration candidate. It does **not** launch a native team or connect to a messaging service.

## Test

From this directory: `python -m unittest discover -s tests -v`. Tests use temporary local files and one **Python-only** Job Object child fixture; no native process, model, messaging service, provider call, or external message is opened for writing. Windows is required.

## Integrated deterministic boundary

- `run_store.py` is the only event writer. It uses an fsynced, checksum-linked log and refuses protected paths, partial/corrupt logs, altered seeds, a second writer and integrity-field collisions. Restarted stores are paused.
- `operator_control.py` (not `operator.py`, which would shadow Python's standard-library module) provides foreground-only `pause`, `stop`, `reject`, `reassign`, `resume` and `status` handling. It freezes before a stop attempt and writes a derived, atomic status projection.
- `mail_adapter.py` accepts only injected fake transports in this candidate. It deduplicates durable inbox actions, blocks cursor anomalies and uncertain sends, and persists the outbox intent before any transport call.
- `native_adapter.py` binds seats to distinct sessions, filters fixture events to safe metadata/final text, and marks service or Pi-permission uncertainty as partial rather than verified.
- `controller.py` composes those boundaries. It writes intent before invocation, blocks unacknowledged/stale work, and never dispatches from recovery.

The same-user cooperative trust model remains a limitation. `status.json` intentionally labels hostile same-user isolation and live native permissions as unknown.

## Component boundaries

- `authority.py`: pure state reducer plus single-writer, checksum-linked/fsynced transition journal. On restart the journal starts **paused**. A host controller must hold private process-local capability objects bound to operator, manager invocation and executor identities and pass them to `Journal.execute(..., caller=handle)`. Never accept an `origin` supplied by Mail, task text, or an agent's assertion. The exposed pure `apply()` is **not an authenticated endpoint**; it is used to replay already validated journal events. There is no installed operator CLI or cross-process authentication yet. Same-user hostile processes are outside this candidate's threat model.
- `mail_queue.py`: persists incoming application IDs, ordered work intents, cursor commits and outgoing application IDs. No `authorize` callback => nothing dispatches. A callback must verify the current journal owner/ACK/epoch/budget **before every reservation**; a Mail ACK alone is not completion. Reopening pauses; uncertain native intent or uncertain remote send returns a typed blocked reason. Operator must review history, job liveness and artifacts before any new dispatch. There is **no network Mail adapter or resume API** yet.
- `completion.py`: `persist_completion()` takes only pre-filtered, project-safe final text; rejects obvious bearer headers and arbitrary metadata, but its narrow regex **cannot establish general secret redaction**. A future native-capture allowlist must run before calling it. Artifacts are create-only/fsynced before UTF-8-limited notification; sensitive input should not be passed into this API. Mail outbox integration is currently a caller contract, not wired together.
- `native_supervisor.py`: explicit OpenCode/Pi session argument builders, Windows suspended-start/Job limits and local stop. The test verifies a contained Python child dies and its counter stays stable, **not** a Pi-origin child or actual native tool permission. The `Supervisor` currently has no live service/listener discovery, persistent Job-handle restart recovery or controller-led native event capture. Consequently `request_stop()` leaves service verification unknown and **does not itself authorize handoff**. A caller must reconcile exact outgoing-seat shutdown plus new authority epoch/ACK; never rely on elapsed time alone. Freeze timing in the fixture is not a measured live stop-latency guarantee.
- `acceptance.py`: all declared scopes and checks must independently approve the same exact revision/epoch. `manifest()` includes brief and tests when the caller supplies the complete snapshot directory. Human rejection withdraws dependent acceptance while retaining history; already-integrated work must use a separate corrective plan. The candidate does not merge/push/deploy.

## Remaining gates before any live or real-project use

Live validation remains separately gated: pin exact versions and limits, implement a trusted local operator channel, verify native event filtering and outbox/ACK crash ordering, and confirm safe allowed-path artifacts. No global configuration edits, credential copying, automatic cleanup, or automatic improvement promotion are part of this candidate.
