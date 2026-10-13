import {defineConfig} from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins:[vue()],build:{outDir:'../src/qqfarm/resources/web',emptyOutDir:true},server:{host:'127.0.0.1',proxy:{'/api':'http://127.0.0.1:8790'}}})
