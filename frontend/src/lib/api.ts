// The one place the frontend talks to the orchestrator.
//
// Every request goes to a relative /api path; the dev server proxies it and a
// reverse proxy does the same in production. No host or port appears here.

export type RunState =
  | "created"
  | "specifying"
  | "spec_review"
  | "building"
  | "testing"
  | "deploy_review"
  | "deploying"
  | "deployed"
  | "failed"
  | "cancelled";

export type Gate = "spec" | "deploy";

export interface Run {
  id: string;
  brief: string;
  title: string | null;
  state: RunState;
  failure_reason: string | null;
  open_gate: Gate | null;
  is_terminal: boolean;
  created_at: string;
  updated_at: string;
}

export interface RunEvent {
  id: string;
  kind: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// Every fetch carries a timeout. An orchestrator that has stopped answering
// must surface as an error, not as a spinner that never resolves.
const TIMEOUT_MS = 15_000;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);

  try {
    const response = await fetch(`/api${path}`, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });

    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { error?: string };
      throw new ApiError(response.status, body.error ?? `${response.status} on ${path}`);
    }

    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(0, `the orchestrator did not answer within ${TIMEOUT_MS / 1000}s`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  listRuns: () => request<Run[]>("/runs"),
  getRun: (id: string) => request<Run>(`/runs/${id}`),
  getEvents: (id: string) => request<RunEvent[]>(`/runs/${id}/events`),

  createRun: (brief: string, title?: string) =>
    request<Run>("/runs", {
      method: "POST",
      body: JSON.stringify({ brief, title: title ?? null }),
    }),

  decideGate: (id: string, gate: Gate, approved: boolean, decidedBy: string, note?: string) =>
    request<Run>(`/runs/${id}/gates/${gate}`, {
      method: "POST",
      body: JSON.stringify({ approved, decided_by: decidedBy, note: note ?? null }),
    }),

  cancelRun: (id: string, actor: string, reason: string) =>
    request<Run>(`/runs/${id}/cancel`, {
      method: "POST",
      body: JSON.stringify({ actor, reason }),
    }),
};
