# External planner authority and FSM-only execution

Status: accepted; supersedes ADR 0005 and the Normalized Plan portions of ADR 0008.

MiniZinc instance checking and solver execution are authoritative for MiniZinc files, while VAL is authoritative for PDDL domain/problem checking and validation of Fast Downward's exact `sas_plan`. Planner-native plans remain referenced artifacts in a Planner Plan envelope; code does not normalize or independently reinterpret their assignments, actions, costs, or dependencies. Hyper binds the accepted planner evidence to an agent-authored Statechart, and execution receives only the activated Statechart/FSM semantics plus Mission and revision correlation, current environment and belief data, and available recipients.
