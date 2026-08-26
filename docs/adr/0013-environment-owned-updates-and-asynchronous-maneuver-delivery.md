# Environment-owned updates and asynchronous Maneuver delivery

Status: accepted; extends ADRs 0010–0012. Implementation tracked by GitHub issue #54.

Environment update ownership is selected by the Environment Profile, and Maneuver Commands are delivered asynchronously through durable transport to an environment-side consumer. Transport receipts and consumer acknowledgement prove durable delivery only; Maneuver Feedback is the sole authority for active, completed, failed, or cancelled physical lifecycle state. In environment-driven mode, immediate replan reconciliation means Context Coordination adds no deliberate delay, although independently advancing Mission time means the replacement heartbeat need not share the requesting evidence timestamp. Stale-decision fencing remains intentionally deferred until the harness measurements of evidence time, completion time, update batches, and coalescing can inform its design.
