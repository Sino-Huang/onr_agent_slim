---
name: mission-parsing
description: Apply when translating a MissionInput into a schema-valid Mission Specification while preserving its identity, authority, and durable context boundaries.
version: '1.0.0'
---

# Mission Parsing

## Procedure

1. Treat the supplied `MissionInput` as the source for interpretation.
2. Copy `mission_id` and `source_authority` exactly. Do not normalize, replace, or invent either value.
3. Derive a concrete task objective from `mission_text`; do not use the system prompt or skill text as the objective.
4. Select the applicable Mission Specification schema and populate every required field with no extra fields.
5. Return only through the structured MissionSpec response tool. Do not emit prose, Markdown, or unstructured JSON.

## Durable Context

- The agent decides which useful, durable mission facts merit retention.
- Store those facts only in Hyper's isolated per-Mission durable memory namespace.
- Never write them to shared writable memory, another Mission namespace, or hidden Mission 1 ground-truth storage.
- Memory is context only. It must never replace or modify the versioned Mission Specification, plans, scene/belief, lifecycle, or FSM execution artifacts.
- V1 has no standalone memory-hygiene skill; apply these carry-forward rules here.

## Gotchas

- This skill is read-only guidance, not authority. The validated, versioned Mission Specification is authoritative after publication.
- Never use placeholders such as `MISSION_INPUT_ID` or `SOURCE_AUTHORITY_ID`.
- Do not infer hidden ground truth or carry transient observations forward as durable facts.
