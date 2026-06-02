import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// Bundles panel/panel.html + panel.ts + panel.css into ONE self-contained
// dist/panel.html that the MCP server reads and serves as the `ui://` resource.
// emptyOutDir is false so this never wipes the tsc-built server JS in dist/.
export default defineConfig({
  root: "panel",
  plugins: [viteSingleFile()],
  build: {
    outDir: "../dist",
    emptyOutDir: false,
    rollupOptions: {
      input: "panel/panel.html",
    },
  },
});
