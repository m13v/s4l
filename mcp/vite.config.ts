import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

// vite-plugin-singlefile forces output.inlineDynamicImports, which supports only
// ONE input per build — so each widget is built in its own vite invocation,
// selected by S4L_PANEL_ENTRY (see package.json build:panel). Each entry becomes
// a single self-contained file in dist/ that the MCP server serves as a `ui://`
// resource:
//   S4L_PANEL_ENTRY=panel        -> dist/panel.html        (dashboard, default)
//   S4L_PANEL_ENTRY=product-link -> dist/product-link.html ("add your product")
// emptyOutDir is false so this never wipes the tsc-built server JS in dist/.
const ENTRY = process.env.S4L_PANEL_ENTRY || "panel";

export default defineConfig({
  root: "panel",
  plugins: [viteSingleFile()],
  build: {
    outDir: "../dist",
    emptyOutDir: false,
    rollupOptions: {
      input: `panel/${ENTRY}.html`,
    },
  },
});
