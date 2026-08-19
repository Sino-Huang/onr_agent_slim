# Bound generated planner assets with code-owned verification

Status: accepted

Planner Asset Generators may propose MiniZinc files and a normalization template from Mission Intent and snapshot-authorized scene evidence, but the code-owned Planner Translator controls a bounded correction loop. MiniZinc's instance checker supplies the static gate; feedback exposes only the failed validation stage and a fixed safe message. Solver output becomes a Normalized Plan only when MiniZinc reports `OPTIMAL_SOLUTION` and the independent assignment checker joins every result to the generated template without disagreement.

During the migration, the translator constructs the legacy Mission Specification compatibility envelope only after external solver execution succeeds. It never publishes or uses that envelope as pre-planner authority; issue #45 removes the compatibility form after provenance-only plan consumers have migrated.
