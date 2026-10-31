import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `npm run dev` proxies /api to a locally running backend (uvicorn on :8080).
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/api': process.env.NIGRANI_API || 'http://localhost:8080' } },
  build: { outDir: 'dist', sourcemap: false },
});
