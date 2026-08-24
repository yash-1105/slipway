import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// No hardcoded host or port. The dev server's own address and the API it
// proxies to both come from the environment, with defaults that only apply on
// a laptop.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.SLIPWAY_API_URL ?? "http://127.0.0.1:8000";

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: env.SLIPWAY_FRONTEND_HOST ?? "127.0.0.1",
      port: Number(env.SLIPWAY_FRONTEND_PORT ?? 5173),
      proxy: {
        // The frontend never learns the API's address: it calls /api and the
        // dev server (or the reverse proxy, in production) resolves it.
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
  };
});
