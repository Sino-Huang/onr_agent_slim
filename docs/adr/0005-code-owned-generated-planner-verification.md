# Bound generated planner assets with code-owned verification

Status: superseded by ADR 0009

Planner Asset Generators may propose MiniZinc or PDDL files and a normalization template from Mission Intent and snapshot-authorized environment data, but the code-owned Planner Translator controls a bounded correction loop. MiniZinc's instance checker supplies its static gate; correction feedback exposes the exact planner process diagnostic with host paths converted to the agent's virtual filesystem namespace, or the precise code-owned invariant that rejected an otherwise successful result. Raw process streams remain immutable verification evidence and host filesystem paths are never part of the model-visible tool interface. Solver output becomes a Normalized Plan only when MiniZinc reports `OPTIMAL_SOLUTION` and the independent assignment checker joins every result to the generated template without disagreement.

Fast Downward's translation-only mode supplies the PDDL static gate. A returned symbolic plan remains non-executable until an independent VAL process accepts the exact persisted domain, problem, and plan artifacts and the code-owned action checker joins every action and cost to the generated normalization template.

After external solver execution and code-owned verification succeed, the translator constructs Plan Provenance and a provenance-only Normalized Plan directly.
