# Trace view contract

The viewer consumes evidence and never owns mission or runtime authority.
`TraceProjection` accepts only documented public v1 records. Heterogeneous
ingestion uses the exact v1 public observation envelope:
`schema_version`, `observation_sequence`, `observed_at`, and `record`.
`observation_sequence` is a positive mission-scoped merge sequence and
`observed_at` is timezone-aware. Source-local sequences remain source metadata.

## Field sources and authority

| Public source | Viewer identity | Viewer payload |
| --- | --- | --- |
| `TransportEvent` | source event identity; component/authority from a fixed event-kind map | allowlisted operational payload fields |
| `Command` | `command:<command_id>`; component `command-source`, authority `command` | target, command kind, and allowlisted command fields |
| `CommandReceipt` | `receipt:<command_id>`; component/authority `transport` | command ID, target, and source business status |
| `CommandOutcome` | `outcome:<command_id>`; component `command-target`, authority `command-outcome` | allowlisted outcome fields and source business status |
| `OperationalLogRecord` | record identity and source-local sequence; fixed public service component, authority `operational-log` | operational-log safe detail keys |
| `SummaryArtifact` | summary identity and source-local sequence; component `mission-log-summarizer`, authority `non-authoritative-summary` | summary, prior IDs, and exact input range |
| validated `ManeuverFeedback` | code-owned `feedback:<feedback_id>`; component `environment` and authority `environment-feedback`, or component `maneuver-control` for an explicit Maneuver Control source | feedback/maneuver IDs, lifecycle, source, command, plan, and snapshot references |
| validated `ReplanRequest` | code-owned `replan-request:<request_id>`; component/authority `hyper-agent` | request ID, safe reason/status, observed/source revisions, and snapshot reference |
| current `BayesianBeliefSnapshot` | code-owned belief identity; component `environment`, authority `bayesian-belief-source` | hash-bound immutable reference, belief/input revisions, and public marginals |
| `MissionSnapshot` | mission/version identity; component `context-coordination`, authority `derived-snapshot` | immutable references, revisions, health, freshness, and missing sources |
| `Statechart`, `FSMStatus`, `FSMExecutionRecord` | deterministic mission/revision identity; component `fsm-runner` | validated declarative or published FSM fields |

Transport event kinds map through code-owned identities for Hyper Agent,
planner, Context Coordination, FSM Runner, Maneuver Control, Maneuver Adapter,
environment, transport, and advisory context. Caller `component` and `authority`
values are not accepted. Source `status` remains business state;
`replay_disposition` is limited to `normal`, `duplicate`, `replayed`, `stale`,
`gap`, `resynchronized`, `conflict`, and `malformed`.
Both legacy snapshots and the current schema are accepted. Current snapshots add
the validated `source_hashes` map to the typed payload. If a transport snapshot
uses the same canonical identity as its derived snapshot, the raw envelope is
assigned a distinct `transport:` identity so the two valid views cannot create
false duplicate or conflict evidence.

## Redaction and diagnostics

Projection begins with per-record allowlists. A recursive defense removes prompt,
completion, reasoning, `analysis`, `text`, `messages`, credential, secret, and
Mission Memory paths and redacts credential-shaped strings. The boolean
`mission_memory_isolated` marker is public; Mission Memory content is not.
Missing evidence uses type-specific `missing_fields`.
The same Bayesian-belief identity applies to `risk.observed`,
`belief.constraints`, and `belief.updated`; only their typed public observation,
constraint, revision, reference, health, marginal, and `content_sha256` fields
enter the common transport projection. Trace reasoning and secret redaction is
unchanged.

Error evidence never renders source strings. It contains only a fixed public
error code/message/category (`malformed_json`, `non_mapping`,
`unsupported_schema`, `unknown_fields`, `invalid_record`, `unsupported_shape`,
or `envelope_required`) and a stable evidence hash. Source field names,
identities, malformed values, schema values, and parser/contract exception text
cannot enter error fields, payloads, statuses, messages, or markers.

## Replay policy

Records are canonicalized before replay analysis. Enveloped records sort by
mission, `observation_sequence`, `observed_at`, and stable event ID, independent
of input permutation. Raw records remain supported for one source type only;
mixing source types, or mixing raw and enveloped records, emits
`envelope_required`. The envelope is the only cross-source merge mechanism.

Identical repeated event IDs retain `duplicate` evidence. Conflicting variants
select the lexicographically smallest canonical projection and emit stable hash-
identified `conflict` evidence. Repeated mission/stream sequences are retained
as `replayed`; missing sequences emit `gap` evidence. Public resync events define
a new floor, preserve `resynchronized`, and mark earlier records `stale` without
re-reporting pre-floor gaps.

## Fixture and summaries

`src/onr/viewer/fixtures/mission_trace.jsonl` is one contiguous sequence of typed
v1 observation envelopes. It covers mission overview, planning and Normalized
Plan, Context Coordination, snapshots, FSM/Statechart, maneuver control/adapter,
scene graph, fan-out, correlated command lifecycle, both feedback loops, Role
Skills advisory context, Mission Memory isolation, Human Question, all physical
actions, one valid non-physical choice, and explicit redaction/missing evidence.

The causal public chain is `bayesian-belief:1` source fact, Mission Snapshot
version 2 referring to that belief, then a Maneuver Control decision. The
`SummaryArtifact` preserves summary text, prior IDs, `input_start_sequence`, and
`input_end_sequence`; summary unavailability is typed operational-log evidence.

## Runtime lease

The runtime owns `RuntimeLeaseStore` in `onr.runtime.lease`. The canonical path is
`config.storage.root / "runtime" / "lease.json"` for direct and factory-created
`RuntimeComposition` instances. Sessions touch `last_seen` at
`min(timeout / 3, 5s)` by default and stop their own lease in `finally`.

A sibling `lease.lock` uses Linux `fcntl.flock` to serialize the complete
start/touch/stop read-check-write transition across processes; `lease.json` is
published by atomic replace. No operation overwrites or stops a different active
owner. Missing, corrupt, stopped, and stale leases are inactive. Viewer code may
read serialized liveness but does not start, touch, stop, or own a lease.
`RuntimeLeaseStore.inspect()` is the viewer read path: it opens only an existing
`lease.json`, does not acquire or create `lease.lock`, and does not create the
runtime directory. Writer transitions remain process-locked.

### Mission summary cadence

`RuntimeComposition.mission_session(mission_id)` owns one mission-level summary
worker for the lifetime of an active runtime lease. The worker is one
non-daemon thread and one stop event. It waits the full configured
`heartbeats.summary_seconds` cadence before its first periodic heartbeat, never
overlaps model calls, and performs one final flush after mission producers leave
the session but before the lease stops. `run_mission()` enters this session
automatically.

Every heartbeat reads the mission's single ordered operational log, so records
from Hyper Agent, Context Coordination, FSM Runner, Maneuver Control, adapters,
environment, transport, planner, and runtime contribute to one non-authoritative
mission digest. Successful summaries are written atomically as
`config.storage.root/summaries/<mission>/<sequence>.json` with an incremental
input sequence range. The runtime emits no success log record, avoiding a
self-generating summary stream.

Summary model calls use a finite 120-second request timeout with one retry.
Summary construction or invocation failure does not change the mission result
or advance the summary cursor. It emits only allowlisted `summary-unavailable`
operational evidence containing `operation=mission_summary` and the exception
type, then retries on the next cadence or final flush. Summary text remains
non-authoritative; the viewer only reads the atomic public artifact while the
runtime lease is active.

## Live server contract

The local server binds only to `127.0.0.1`, `::1`, or a `localhost` argument
normalized to `127.0.0.1`. It accepts only `GET` and `HEAD`. Static content is
contained below `src/onr/viewer/web/`, has no directory listing, and is served
with a same-origin-only Content Security Policy.
Every request is rejected before route or static-file handling unless exactly
one `Host` header matches the bound loopback authority and port (`[::1]:port`
for IPv6). If an `Origin` header is present, exactly one value must resolve to
the same HTTP origin. This boundary also protects the raw local debug endpoint.
When and only when the server was explicitly requested with `--host localhost`,
the matching `localhost` authority is retained alongside the normalized
`127.0.0.1` listener authority. Default HTTP port authorities may omit `:80`;
IPv6 authorities remain bracketed. No other DNS host alias is accepted.

While the lease is active, `GET /api/runtime` returns `active`, public lease
timestamps, and a sorted `mission_ids` list derived only from validated public
artifacts. It never returns the session ID, PID, config, repository root,
storage paths, service credentials, or secrets. Missing, corrupt, stopped, or
stale liveness returns only `{"active":false}`.

`GET /api/trace?mission_id=<id>` requires exactly one mission ID matching the
public component grammar `[A-Za-z0-9][A-Za-z0-9._:-]{0,255}`. Missing, repeated,
invalid, traversing, unavailable, or inactive selections return
`{"items":[]}`. A response contains records for exactly the selected mission;
missions are never merged. The server checks the same lease session again after
artifact collection and discards the collected result if it stopped, expired,
or was replaced.

### Public live artifact set

The server reads only the following bounded set. Under `config.transport.root`,
`<mission>` and other components use the percent-encoding applied by
`FileTransport`. Under `config.storage.root`, `<mission>` is the literal raw
mission ID directory used by the operational log, summarizer, and FSM store.
For example, `mission:alpha` is `mission%3Aalpha` in transport trees and
`mission:alpha` in storage trees. Every directory, filename, embedded mission
ID, and typed record must agree before projection.

| Root | Public files |
| --- | --- |
| `config.transport.root` | `topics/<topic>/missions/<mission>/*.json` as validated `TransportEvent` envelopes |
| `config.transport.root` | `commands/<service>/<mission>/*.json` as validated command-stream envelopes containing `Command` or `CommandOutcome` |
| `config.transport.root` | `receipts/<command_id>.json`, reached only from a validated command and validated as its matching `CommandReceipt` |
| `config.storage.root` | `operational-log/<mission>/events/[0-9]*.json` as `OperationalLogRecord` |
| `config.storage.root` | `summaries/<mission>/[0-9]*.json` as canonical `SummaryArtifact`; `cursor.json` is excluded |
| `config.storage.root` | `fsm/<mission>/statechart.json` as untrusted `Statechart` and `fsm/<mission>/execution-record.json` as `FSMExecutionRecord` |
| `config.storage.root` | the current `bayesian-beliefs/<mission>` snapshot returned by `FileBayesianBeliefStore.load_current_read_only()` |

Known typed transport payloads are additionally validated with their existing
contracts: `MissionSnapshot`, `FSMStatus`, `Statechart`, `FSMExecutionRecord`,
`ManeuverFeedback`, and advisory `ReplanRequest`. Their transport envelope is
retained so source event, correlation, and payload identity remain visible;
snapshot, FSM, feedback, and replan payloads also produce bounded typed
projections needed by their UI views. Feedback promotes only its IDs, lifecycle,
explicit source, command/correlation/parent links, and plan/snapshot references.
Replan promotes only its request identity, safe reason/status, revision
references, and code-owned correlation/parent links. Neither typed projection
passes through the original nested payload. Other topic records remain validated public
`TransportEvent` envelopes and pass through the projection allowlists.

The belief row uses `load_current_read_only()`: it performs the same committed
pointer, checkpoint, generation, artifact, revision, and hash binding validation
without pruning partial generations, history, content-addressed artifacts, or
any private file. Viewer `GET` therefore never performs belief recovery or
storage maintenance; those remain writer responsibilities.

No `identity/`, `subscriptions/`, dead-letter, planner scratch, Mission Memory,
role context, arbitrary storage, symlinked directory, private belief generation,
checkpoint, pending-output, committed-pointer detail, or raw artifact endpoint is
part of the live artifact set. Invalid, unbound, corrupt, or unavailable files
are omitted.
All viewer JSON mapping reads, including command receipts, open the selected file
once beneath the configured transport or storage root using directory-file-
descriptor traversal. The root, every intermediate component, and the final
file are opened with no-follow semantics; symlinked topic, mission, event,
receipt, or storage trees cannot redirect reads outside the configured root.
The final descriptor must be a bounded regular file and only its bytes are
parsed. Duplicate keys, non-finite constants, oversized files, malformed
UTF-8/JSON, excessive recursion, and symlinks are omitted without a second
path-based read.
Typed absence is shown only when a supported public record, such as an
operational `summary-missing` event or snapshot `missing_sources`, explicitly
declares it.

Each selected mission is ordered deterministically by source class, source-local
sequence, stable identity, and canonical public record. The server assigns the
resulting contiguous order to the v1 `observation_sequence`; no filesystem time
or directory enumeration order is authoritative.

## Local debug projection

`GET /api/debug?mission_id=<id>` remains gated by `config.debug` and the same
runtime lease identity checks. It accepts an optional, single `role` selected
from `hyper-agent`, `maneuver-control`, `mission-summary`, and `runtime`.
Unknown, repeated, blank, or additional query parameters return the safe empty
debug response. With no role, the response remains a cross-role view and adds
`conversations` alongside `profiles` and `invocations`; with a role, all three
collections are filtered to that scope.

Agent artifacts are read from
`debug/agent/<role>/<percent-encoded-mission>/`, with exact v1 profile and
invocation validation. Legacy `debug/agent/<percent-encoded-mission>/` files are
accepted as a deduplicated fallback and receive their validated `agent_role` as
the explicit role. Raw LLM conversations are read only from
`debug/llm/<role>/<percent-encoded-mission>/<sequence>.json`. Each file must have
the exact 12-key `LLMResponseRecorder` schema and a numeric sequence filename.
The projection exposes role/sequence, model and request messages, provider
reasoning fields, content/function/tool output, and finish/status metadata. No
invocation-to-response linkage is inferred; ordering is role then sequence.

Debug files retain validated local values, including provider reasoning, and do
not pass through trace payload redaction. This is an intentional trust boundary:
the server is loopback-only, the endpoint is disabled unless runtime debug is
enabled, request headers and credentials remain excluded by the recorders, and
debug data is not part of the public trace artifact set.
