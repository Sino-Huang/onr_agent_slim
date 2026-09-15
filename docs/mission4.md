# Mission 4: worker-requested object search

For the terminal-audited vLLM/runtime launcher and durable worker request pane,
see [Model-backed live demos for Missions 2–4](live-demo-missions-2-4.md).

Parent specification: [runtime #27](https://github.com/Sino-Huang/onr_physical_runtime/issues/27).
Runtime contracts/examples live in the sibling `onr_physical_runtime` checkout's
`docs/mission_desc/mission4_contract.md`. Agent modules do not import runtime,
private sensor fixtures or evaluator truth.

## Belief-manager extension (#31)

`BayesianBeliefManager.for_object_search(mission_id, package)` constructs the
categorical object-search extension. Its `ingest(mission4, mission_time_s)` accepts
the immutable public `world_model_info.mission4` section and returns a frozen
`SearchBeliefSnapshot` of typed `SearchMatch` records. The Agent owns accumulated
matching and found decisions; raw `info[0]` retains local observation uncertainty.

The existing generic binary-risk SIR manager/service and ADR 0002 provenance
remain unchanged. Object search adds an exact finite categorical update in the
same belief manager for explicitly declared attribute vocabularies. It does not
publish categorical data as an ADR 0002 binary-risk snapshot or bypass Context
Coordination with a `belief.updated` payload. #32 integrates the typed search
results through the Mission 4 planning path.

Each observed track maintains a distribution per observed attribute. Priors are
uniform over declared values. Under the runtime's explicitly named symmetric
categorical error model, a reading has likelihood `1-u` for its reported value and
`u/(K-1)` for alternatives. Distinct samples update those distributions once.
Attributes are treated as conditionally independent in this behavioral baseline;
complete-description probability is their product. This assumption is disclosed,
not a calibration claim for the colleague's future model. Null local uncertainty
supplies no likelihood; unobserved required attributes keep a match unresolved.
Contradictory zero-likelihood evidence becomes unresolved rather than certain.

Track identity is the producer's observed association, not a hidden target ID.
The current contract does not invent association probabilities absent from the
producer. Added objectives reuse existing attribute distributions and evidence.
Amended descriptions query those distributions without assimilating samples again.
Removal preserves historical reports but drops current objective decisions.

A found decision requires complete-match probability strictly greater than
`1-found_threshold` (default 0.9), plus supported localization. Equality fails.
The chosen location is the sample with the smallest reported uncertainty radius,
breaking ties by newest acquisition time. Its radius must be available, no more
than 5 m, and its uncertainty disk must fit inside an allowed polygon. This is
evidence support, not knowledge of actual localization error; private evaluation
can still mark a found claim wrong. Confident nonmatches never qualify.

Report history retains supporting sample IDs, sample/publication times, candidate
and target identities, request revision, location and accumulated uncertainty.
New evidence may update a found report or withdraw it. Repeated snapshots do not
create observations, belief changes or duplicate reports. `ObjectSearchBeliefStore`
atomically checkpoints distributions, consumed observations, revision state and
reports before the caller publishes them; resume validates mission/package scope.

Validation:

```bash
pytest -q tests/test_object_search_belief.py tests/test_bayesian_belief.py \
  tests/test_reporting_reliability.py
```

The tests cover useful additional views, strict threshold boundaries, nonmatches,
unknown uncertainty, unsupported/out-of-area locations, contradiction, request
changes, old-evidence reuse and checkpoint replay. Worker interaction/planning is
#32; actual runtime/Agent acceptance is #33; native perception acceptance is #35.

## Worker requests and adaptive planning (#32)

`Mission4WorkerSession` durably queues worker text and submits one revision at a
time using the runtime's documented `search_requests/` boundary. Runtime receipts
acknowledge acceptance; queued text is retained across restart while a request is
pending. Text parsing does not reset or directly mutate runtime mission state.

Supported examples:

- `Please find a red container in dock` — adds a target.
- `also find a blue truck` — adds another target across configured areas.
- `change worker:1 to blue container in dock` — explicit description/area amendment.
- `extend deadline to 400 seconds` — absolute mission-time deadline change.
- `remove worker:2`, `cancel search`, `show progress`, `show evidence`.

Each target accepts one value per named attribute from the configured vocabulary.
Unknown words, negation, competing values and unknown areas require clarification.
This is a deterministic constrained-language intake, not a full-LLM language quality
claim. Clarification leaves accepted work intact. Requests after termination require
a new Mission Run. Progress/evidence queries produce no objective revision; the
caller renders current per-target belief/report state.

`Mission4AdaptivePlanner.decide(environment)` consumes actual public environment
snapshots, updates Agent-owned beliefs and returns typed decisions. It selects areas
jointly for unresolved objectives using priors, travel and observed coverage; observed
candidates can prompt additional cardinal views. It preserves still-useful active
legs and handles completed/failed feedback, changed objectives and final outcomes.
It reuses existing `navigate`/`search_area` command parameters, including arrival
direction for a useful camera-facing view. A finite set of additional viewpoints
bounds repeated investigation; unresolved targets remain explicit when search is
exhausted. Public geometry and runtime execution enforce restrictions. Private
target coordinates/labels never enter the planner.

`Mission4ReplanGate` is wired into Context Coordination alongside existing mission
gates. It wakes planning on a new search decision and stays inactive for other
mission modes. Physical command completion still comes from environment feedback.
The standalone acceptance runner in #33 drives the same policy over actual Agent
transport, with deterministic planning labelled explicitly.

Bounded, nonmutating checks on a captured public environment JSON:

```bash
python -u -m onr.application.mission4_planning environment.json --dry-run
python -u -m onr.adapters.mission4_worker --mission-id mission4 \
  --session var/mission4-worker.json --request-directory var/runtime/search_requests \
  --environment environment.json --text 'find red container in dock' --dry-run
```

Remove `--dry-run` from the worker command to queue/publish its request. The session
owner calls `advance` on subsequent public snapshots to consume acknowledgements
and publish queued requests. `examples/mission4_requests.json` supplies a two-request
script for the later integration runner. Full loop commands and evidence belong to
#33; these checks do not claim that flight or integration has run.
