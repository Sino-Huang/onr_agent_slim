# Mission 4: worker-requested object search

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
