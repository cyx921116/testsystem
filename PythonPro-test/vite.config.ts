import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  root: `${projectRoot}src/testsystem/frontend`,
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: `${projectRoot}src/testsystem/static`,
    emptyOutDir: false,
    lib: {
      entry: `${projectRoot}src/testsystem/frontend/FlowEditor.tsx`,
      name: "PilotFlowEditor",
      formats: ["iife"],
      fileName: () => "flow-editor.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: "flow-editor.css",
      },
    },
  },
});
