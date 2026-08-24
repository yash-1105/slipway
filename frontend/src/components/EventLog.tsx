import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";

export function EventLog({ runId }: { runId: string }) {
  const events = useQuery({
    queryKey: ["runs", runId, "events"],
    queryFn: () => api.getEvents(runId),
  });

  if (events.isPending) return <p className="text-sm text-slate-500">Loading the log…</p>;
  if (events.isError) {
    return <p className="text-sm text-red-700">{(events.error as Error).message}</p>;
  }

  return (
    <ol className="space-y-1 font-mono text-xs">
      {events.data.map((event) => (
        <li key={event.id} className="flex gap-3">
          <time className="shrink-0 text-slate-400">
            {new Date(event.created_at).toLocaleTimeString()}
          </time>
          <span className="shrink-0 font-medium text-slate-700">{event.kind}</span>
          <span className="truncate text-slate-500">{JSON.stringify(event.payload)}</span>
        </li>
      ))}
    </ol>
  );
}
