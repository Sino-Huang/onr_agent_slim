# Operator Debug View shares the viewer projection seam

Status: accepted

Runtime Host API v1.1 exposes a loopback-only Operator Debug View that incrementally projects durable operational evidence, environment state, run-scoped planner Artifacts, and the viewer's existing agent-step parsing rather than adding console-specific callbacks to the agent runtime. Any loopback console may read this view without a Console Session credential, matching the existing public local evidence surface; detailed tool payloads and Recorded Debug Reasoning are present only when `debug: true`. Mission Run Status, FSM and environment evidence remain authoritative in their own domains, while Recorded Debug Reasoning is explicitly labeled non-authoritative and remains outside Run Observations. Older v1.0 consoles and endpoints remain supported, and a v1.1 console deliberately retains the legacy dashboard when connected to a v1.0 Host.
