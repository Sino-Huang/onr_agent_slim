// API client. Every endpoint is tried live first; on 404 (endpoint not yet
// deployed) or a network error (static-only server) the client falls back to
// the bundled mock dataset so the UI is always demoable. Non-404 HTTP errors
// propagate — a broken live endpoint must not be masked by mock data.

import { mockRuntimePayload } from "../mock/runtime.js";
import { mockStepsPayload } from "../mock/steps.js";
import { mockRunPayload } from "../mock/run.js";
import { mockArtifact } from "../mock/artifacts.js";

export const mockUsed = { runtime: false, steps: false, run: false, artifact: false };

async function fetchJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!response.ok) {
    const error = new Error(`GET ${url} → HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function fallsToMock(error) {
  return !error.status || error.status === 404;
}

export async function getRuntime() {
  try {
    const data = await fetchJSON("/api/runtime");
    mockUsed.runtime = false;
    return data;
  } catch (error) {
    if (!fallsToMock(error)) throw error;
    mockUsed.runtime = true;
    return mockRuntimePayload();
  }
}

export async function getSteps(missionId) {
  try {
    const data = await fetchJSON("/api/steps?mission_id=" + encodeURIComponent(missionId));
    mockUsed.steps = false;
    return data;
  } catch (error) {
    if (!fallsToMock(error)) throw error;
    mockUsed.steps = true;
    // The mock always serves the demo story; the requested id is echoed back.
    return mockStepsPayload(missionId);
  }
}

export async function getRun(missionId) {
  try {
    const data = await fetchJSON("/api/run?mission_id=" + encodeURIComponent(missionId));
    mockUsed.run = false;
    return data;
  } catch (error) {
    if (!fallsToMock(error)) throw error;
    mockUsed.run = true;
    return mockRunPayload(missionId);
  }
}

export async function getArtifact(missionId, ref) {
  const url = "/api/artifact?mission_id=" + encodeURIComponent(missionId) + "&ref=" + encodeURIComponent(ref);
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      const error = new Error(`GET ${url} → HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    mockUsed.artifact = false;
    const type = response.headers.get("Content-Type") || "";
    return { ref, text: await response.text(), json: type.includes("json"), fromMock: false };
  } catch (error) {
    if (!fallsToMock(error)) throw error;
    const text = mockArtifact(ref);
    if (text === null) {
      const notFound = new Error(`No mock artifact for ref "${ref}"`);
      notFound.status = 404;
      notFound.hard = true; // genuine miss, not a fallback
      throw notFound;
    }
    mockUsed.artifact = true;
    return { ref, text, json: ref.endsWith(".json"), fromMock: true };
  }
}
