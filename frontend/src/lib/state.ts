import type { RunState } from "./api";

// Mirrors app/domain/entities.py. Presentation only -- the frontend never
// decides what a run may do next; it asks the API and renders the answer.
const LABELS: Record<RunState, string> = {
  created: "Created",
  specifying: "Writing the spec",
  spec_review: "Waiting: spec approval",
  building: "Building",
  testing: "Testing",
  deploy_review: "Waiting: deploy approval",
  deploying: "Deploying",
  deployed: "Deployed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const TONES: Record<RunState, string> = {
  created: "bg-slate-100 text-slate-700",
  specifying: "bg-sky-100 text-sky-800",
  spec_review: "bg-amber-100 text-amber-900",
  building: "bg-sky-100 text-sky-800",
  testing: "bg-sky-100 text-sky-800",
  deploy_review: "bg-amber-100 text-amber-900",
  deploying: "bg-sky-100 text-sky-800",
  deployed: "bg-emerald-100 text-emerald-900",
  failed: "bg-red-100 text-red-900",
  cancelled: "bg-slate-200 text-slate-600",
};

export const stateLabel = (state: RunState): string => LABELS[state];
export const stateTone = (state: RunState): string => TONES[state];

export const ORDERED_STAGES: RunState[] = [
  "specifying",
  "spec_review",
  "building",
  "testing",
  "deploy_review",
  "deploying",
  "deployed",
];
