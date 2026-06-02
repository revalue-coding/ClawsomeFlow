import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Frontend is served standalone in dev (Vite at 5173) and proxies HTTP +
// WebSocket calls to the FastAPI backend on `Config.csflow_port` (default
// 17017). Run `npm run dev` after `csflow start` (or `uvicorn`) on the
// backend.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:17017",
        changeOrigin: false,
      },
      "/ws": {
        target: "ws://127.0.0.1:17017",
        ws: true,
      },
      "/health": "http://127.0.0.1:17017",
    },
  },
});
