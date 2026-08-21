import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 构建产物由后端 FastAPI 静态托管（backend/app/main.py）
export default defineConfig({
  plugins: [vue()],
  base: "/",
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1024,
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      // 开发模式：API 与 WS 代理到后端
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
