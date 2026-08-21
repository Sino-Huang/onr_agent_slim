// Mock /api/runtime payload — mirrors the existing endpoint's shape.

export function mockRuntimePayload() {
  return {
    active: true,
    available: true,
    status: "running",
    started_at: "2026-08-21T09:40:52.000+00:00",
    last_seen: new Date().toISOString(),
    mission_ids: ["mission:demo"],
  };
}
