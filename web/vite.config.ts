import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/ui-v2/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./vendor/shadcn", import.meta.url)),
    },
  },
  build: {
    sourcemap: false,
    assetsDir: "assets",
    emptyOutDir: true,
  },
});
