import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],server:{port:5173,proxy:{'/api':'http://127.0.0.1:8765','/data':'http://127.0.0.1:8765','/media':'http://127.0.0.1:8765','/published':'http://127.0.0.1:8765'}},build:{outDir:'dist',chunkSizeWarningLimit:1000}});
