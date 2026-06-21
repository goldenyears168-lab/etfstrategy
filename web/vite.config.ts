import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { localBriefsPlugin } from "./vite-plugin-local-briefs";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "..", "");
  const useLocal = env.VITE_USE_LOCAL_BRIEFS === "1";

  return {
    plugins: [react(), ...(useLocal ? [localBriefsPlugin()] : [])],
    envDir: "..",
    server: {
      port: 5173,
      fs: { allow: [".."] },
    },
  };
});
