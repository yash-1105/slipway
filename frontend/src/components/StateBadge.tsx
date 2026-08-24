import type { RunState } from "../lib/api";
import { stateLabel, stateTone } from "../lib/state";

export function StateBadge({ state }: { state: RunState }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${stateTone(state)}`}
    >
      {stateLabel(state)}
    </span>
  );
}
