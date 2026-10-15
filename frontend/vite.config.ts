import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';
import {readFileSync} from 'node:fs';
const release=JSON.parse(readFileSync(new URL('../release.json',import.meta.url),'utf8'));
export default defineConfig({plugins:[react()],define:{__PILOT_VERSION__:JSON.stringify(release.app_version),__PILOT_BUILD_ID__:JSON.stringify(release.build_id)},base:'./',server:{port:5182,proxy:{'/api':'http://127.0.0.1:8772'}},build:{outDir:'dist',chunkSizeWarningLimit:1200}});
