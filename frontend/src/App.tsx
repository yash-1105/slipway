import { useState } from "react";

import type { Run } from "./lib/api";
import { RunDetail } from "./routes/RunDetail";
import { RunList } from "./routes/RunList";

// Two developers, one shared deployment. Who is approving is recorded on every
// gate decision, so it is asked for once and kept in the session.
function useApprover(): [string, (value: string) => void] {
  const [approver, setApprover] = useState(
    () => sessionStorage.getItem("slipway.approver") ?? "",
  );
  return [
    approver,
    (value: string) => {
      sessionStorage.setItem("slipway.approver", value);
      setApprover(value);
    },
  ];
}

export function App() {
  const [selected, setSelected] = useState<Run | null>(null);
  const [approver, setApprover] = useApprover();

  if (!approver) {
    return (
      <main className="mx-auto max-w-md p-8">
        <h1 className="text-xl font-semibold">Slipway</h1>
        <p className="mt-2 text-sm text-slate-600">
          Your name is recorded against every gate decision.
        </p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("name");
            if (typeof value === "string" && value.trim()) setApprover(value.trim());
          }}
          className="mt-4 flex gap-2"
        >
          <input
            name="name"
            className="flex-1 rounded border border-slate-300 p-2 text-sm"
            placeholder="Your name"
          />
          <button type="submit" className="rounded bg-slate-900 px-3 py-1.5 text-sm text-white">
            Continue
          </button>
        </form>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-3xl p-8">
      <header className="mb-8 flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Slipway</h1>
        <span className="text-xs text-slate-500">approving as {approver}</span>
      </header>

      {selected ? (
        <RunDetail run={selected} decidedBy={approver} onBack={() => setSelected(null)} />
      ) : (
        <RunList onOpen={setSelected} />
      )}
    </main>
  );
}
