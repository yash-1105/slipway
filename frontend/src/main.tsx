import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { App } from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A run changes state when a worker finishes a job, which the frontend
      // has no way to be told about in V1. Polling is the honest answer; the
      // interval is short because a human is watching a gate.
      refetchInterval: 3000,
      staleTime: 1000,
      retry: 2,
    },
  },
});

const root = document.getElementById("root");
if (!root) {
  throw new Error("no #root element; index.html and main.tsx disagree");
}

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
