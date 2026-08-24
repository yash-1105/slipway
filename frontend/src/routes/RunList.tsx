import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Run } from "../lib/api";
import { StateBadge } from "../components/StateBadge";

export function RunList({ onOpen }: { onOpen: (run: Run) => void }) {
  const [brief, setBrief] = useState("");
  const [title, setTitle] = useState("");
  const queryClient = useQueryClient();

  const runs = useQuery({ queryKey: ["runs"], queryFn: api.listRuns });

  const create = useMutation({
    mutationFn: () => api.createRun(brief, title || undefined),
    onSuccess: (run) => {
      setBrief("");
      setTitle("");
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      onOpen(run);
    },
  });

  return (
    <div className="space-y-6">
      <section className="rounded-lg border border-slate-200 p-4">
        <h2 className="font-medium">New run</h2>
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Client or short label"
          className="mt-3 w-full rounded border border-slate-300 p-2 text-sm"
        />
        <textarea
          value={brief}
          onChange={(event) => setBrief(event.target.value)}
          rows={5}
          placeholder="The brief. What is being built, for whom, and what it has to do."
          className="mt-2 w-full rounded border border-slate-300 p-2 text-sm"
        />
        <button
          type="button"
          disabled={create.isPending || brief.trim().length === 0}
          onClick={() => create.mutate()}
          className="mt-2 rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {create.isPending ? "Starting…" : "Start run"}
        </button>
        {create.isError && (
          <p className="mt-2 text-sm text-red-700">{(create.error as Error).message}</p>
        )}
      </section>

      <section>
        <h2 className="mb-2 font-medium">Runs</h2>
        {runs.isPending && <p className="text-sm text-slate-500">Loading…</p>}
        {runs.isError && (
          <p className="text-sm text-red-700">{(runs.error as Error).message}</p>
        )}
        <ul className="divide-y divide-slate-200">
          {runs.data?.map((run) => (
            <li key={run.id}>
              <button
                type="button"
                onClick={() => onOpen(run)}
                className="flex w-full items-center justify-between gap-4 py-3 text-left hover:bg-slate-50"
              >
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">
                    {run.title ?? run.brief.slice(0, 60)}
                  </span>
                  <span className="block font-mono text-xs text-slate-400">{run.id}</span>
                </span>
                <StateBadge state={run.state} />
              </button>
            </li>
          ))}
        </ul>
        {runs.data?.length === 0 && (
          <p className="py-6 text-sm text-slate-500">No runs yet.</p>
        )}
      </section>
    </div>
  );
}
