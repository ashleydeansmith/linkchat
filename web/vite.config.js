import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The web UI talks to the the parent program engine API (python -m engine serve → 127.0.0.1:8770).
// In dev we PROXY /api there, so the front-end uses relative /api paths with no CORS — and the
// exact same relative paths work in production when the API serves the built files same-origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/api": { target: "http://127.0.0.1:8770", changeOrigin: true },
    },
  },
});
