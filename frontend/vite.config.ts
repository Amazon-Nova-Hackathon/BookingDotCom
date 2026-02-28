import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    strictPort: false,
    allowedHosts: [
      'localhost',
      '.trycloudflare.com',
      '.ngrok-free.app',
      '.ngrok.io',
    ],
    proxy: {
      '/offer': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
      },
      '/ws': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
        ws: true,
      },
      '/events': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
      },
      '/screenshot': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://127.0.0.1:7860',
        changeOrigin: true,
        ws: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    strictPort: false,
  },
})
