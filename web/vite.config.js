import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  server: { proxy: { "/api": "http://127.0.0.1:8001" } },
});
