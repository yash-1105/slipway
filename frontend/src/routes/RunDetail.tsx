import { useQuery } from "@tanstack/react-query";

import { api, type Run } from "../lib/api";
import { EventLog } from "../components/EventLog";
import { GatePanel } from "../components/GatePanel";
import { StateBadge } from "../components/StateBadge";

export function RunDetail({
  run: initial,
  decidedBy,
  onBack,
}: {
  run: Run;
  decidedBy: string;
  onBack: () => void;
}) {
  const run = useQuery({
    queryKey: ["runs", initial.id],
    queryFn: () => api.getRun(initial.id),
    initialData: initial,
  });

  const current = run.data;

  return (
    <div className="space-y-6">
      <button type="button" onClick={onBack} className="text-sm text-slate-500 hover:underline">
        ← All runs
      </button>

      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="truncate text-lg font-medium">
            {current.title ?? current.brief.slice(0, 60)}
          </h2>
          <p className="font-mono text-xs text-slate-400">{current.id}</p>
        </div>
        <StateBadge state={current.state} />
      </header>

      {current.failure_reason && (
        <p className="rounded border border-red-300 bg-red-50 p-3 text-sm text-red-900">
          {current.failure_reason}
        </p>
      )}

      <GatePanel run={current} decidedBy={decidedBy} />

      <section>
        <h3 className="mb-2 text-sm font-medium text-slate-700">Brief</h3>
        <p className="whitespace-pre-wrap rounded bg-slate-50 p-3 text-sm">{current.brief}</p>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-medium text-slate-700">Event log</h3>
        <EventLog runId={current.id} />
      </section>
    </div>
  );
}
