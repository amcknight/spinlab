import { defineConfig } from "vite";

export default defineConfig({
  root: ".",
  base: "/static/",
  build: {
    outDir: "../python/spinlab/static",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "happy-dom",
  },
});
