import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// Bundles each panel/*.html entry + its TS/CSS into ONE self-contained file in
// dist/ that the MCP server reads and serves as a `ui://` resource:
//   panel/panel.html        -> dist/panel.html        (dashboard)
//   panel/product-link.html -> dist/product-link.html ("add your product" widget)
// emptyOutDir is false so this never wipes the tsc-built server JS in dist/.
export default defineConfig({
  root: "panel",
  plugins: [viteSingleFile()],
  build: {
    outDir: "../dist",
    emptyOutDir: false,
    rollupOptions: {
      input: {
        panel: "panel/panel.html",
        "product-link": "panel/product-link.html",
      },
    },
  },
});
