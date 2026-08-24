import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api, type Gate, type Run } from "../lib/api";

const PROMPT: Record<Gate, string> = {
  spec: "Approve the specification and start the build?",
  deploy: "Approve this build and deploy it?",
};

export function GatePanel({ run, decidedBy }: { run: Run; decidedBy: string }) {
  const [note, setNote] = useState("");
  const queryClient = useQueryClient();

  const decide = useMutation({
    mutationFn: ({ approved }: { approved: boolean }) => {
      if (!run.open_gate) {
        throw new Error("no gate is open on this run");
      }
      return api.decideGate(run.id, run.open_gate, approved, decidedBy, note || undefined);
    },
    onSuccess: () => {
      setNote("");
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  if (!run.open_gate) return null;

  return (
    <section className="rounded-lg border border-amber-300 bg-amber-50 p-4">
      <h3 className="font-medium text-amber-950">{PROMPT[run.open_gate]}</h3>

      <textarea
        value={note}
        onChange={(event) => setNote(event.target.value)}
        rows={3}
        placeholder="Why. On a rejection this goes to the agent as its next input."
        className="mt-3 w-full rounded border border-amber-300 bg-white p-2 text-sm"
      />

      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          disabled={decide.isPending}
          onClick={() => decide.mutate({ approved: true })}
          className="rounded bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          // A rejection without a reason gives the agent nothing to work with.
          disabled={decide.isPending || note.trim().length === 0}
          onClick={() => decide.mutate({ approved: false })}
          title={note.trim() ? undefined : "A rejection needs a reason"}
          className="rounded bg-red-700 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          Reject
        </button>
        {decide.isError && (
          <span className="text-sm text-red-800">{(decide.error as Error).message}</span>
        )}
      </div>
    </section>
  );
}
