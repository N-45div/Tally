// Builds Tally's one stylesheet. Pages are rendered by Flask and run no JavaScript, so the only output is CSS (and the
// font it uses): tally/web/static/tally.css and tally/web/static/fonts/. The built files are committed; the host
// needs no Node.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const here = (path) => fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  base: "./", // url()s in the stylesheet stay relative to it, under Flask's /static/
  publicDir: here("public"), // copied as they are: favicon.svg
  plugins: [
    tailwindcss(),
    {
      name: "tally-css-only",
      generateBundle(_options, bundle) {
        // A stylesheet entry still yields an empty script chunk; the pages load no scripts, so drop it.
        for (const [name, file] of Object.entries(bundle)) if (file.type === "chunk") delete bundle[name];
        if (Object.keys(bundle).some((name) => name.startsWith("fonts/"))) {
          this.emitFile({ type: "asset", fileName: "fonts/Inter-OFL.txt",
                          source: readFileSync(here("node_modules/@fontsource-variable/inter/LICENSE")) });
        }
      },
    },
  ],
  build: {
    outDir: here("../tally/web/static"),
    emptyOutDir: false,
    assetsInlineLimit: 0, // no data: URIs: the content policy allows fonts and images from 'self' only
    cssMinify: true,
    rollupOptions: {
      input: here("src/tally.css"),
      output: {
        assetFileNames: (asset) =>
          (asset.names ?? [asset.name]).some((n) => n?.endsWith(".css")) ? "tally.css" : "fonts/[name][extname]",
      },
    },
  },
});
