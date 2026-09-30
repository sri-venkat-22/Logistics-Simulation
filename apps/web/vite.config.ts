import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  build: {
    chunkSizeWarningLimit: 2500,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes("maplibre-gl")) return "maplibre";
          if (id.includes("@deck.gl") || id.includes("@luma.gl") || id.includes("@loaders.gl") || id.includes("@math.gl")) return "deckgl";
          if (id.includes("echarts") || id.includes("zrender")) return "echarts";
          if (id.includes("three") || id.includes("force-graph")) return "three";
        },
      },
    },
  },
});
