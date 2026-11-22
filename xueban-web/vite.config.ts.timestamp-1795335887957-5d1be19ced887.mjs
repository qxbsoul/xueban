// vite.config.ts
import { defineConfig } from "file:///D:/capital%20lion/xueban/xueban-web/node_modules/vite/dist/node/index.js";
import react from "file:///D:/capital%20lion/xueban/xueban-web/node_modules/@vitejs/plugin-react/dist/index.js";
var vite_config_default = defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    allowedHosts: true,
    port: 3007,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.VITE_GATEWAY_URL || "http://localhost:8002",
        changeOrigin: true
      }
    }
  },
  preview: {
    host: "0.0.0.0",
    allowedHosts: true
  }
});
export {
  vite_config_default as default
};
//# sourceMappingURL=data:application/json;base64,ewogICJ2ZXJzaW9uIjogMywKICAic291cmNlcyI6IFsidml0ZS5jb25maWcudHMiXSwKICAic291cmNlc0NvbnRlbnQiOiBbImNvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9kaXJuYW1lID0gXCJEOlxcXFxjYXBpdGFsIGxpb25cXFxceHVlYmFuXFxcXHh1ZWJhbi13ZWJcIjtjb25zdCBfX3ZpdGVfaW5qZWN0ZWRfb3JpZ2luYWxfZmlsZW5hbWUgPSBcIkQ6XFxcXGNhcGl0YWwgbGlvblxcXFx4dWViYW5cXFxceHVlYmFuLXdlYlxcXFx2aXRlLmNvbmZpZy50c1wiO2NvbnN0IF9fdml0ZV9pbmplY3RlZF9vcmlnaW5hbF9pbXBvcnRfbWV0YV91cmwgPSBcImZpbGU6Ly8vRDovY2FwaXRhbCUyMGxpb24veHVlYmFuL3h1ZWJhbi13ZWIvdml0ZS5jb25maWcudHNcIjtpbXBvcnQgeyBkZWZpbmVDb25maWcgfSBmcm9tICd2aXRlJztcclxuaW1wb3J0IHJlYWN0IGZyb20gJ0B2aXRlanMvcGx1Z2luLXJlYWN0JztcclxuXHJcbi8vIGh0dHBzOi8vdml0ZWpzLmRldi9jb25maWcvXHJcbmV4cG9ydCBkZWZhdWx0IGRlZmluZUNvbmZpZyh7XHJcbiAgcGx1Z2luczogW3JlYWN0KCldLFxyXG4gIHNlcnZlcjoge1xyXG4gICAgaG9zdDogJzAuMC4wLjAnLFxyXG4gICAgYWxsb3dlZEhvc3RzOiB0cnVlLFxyXG4gICAgcG9ydDogMzAwNyxcclxuICAgIHN0cmljdFBvcnQ6IHRydWUsXHJcbiAgICBwcm94eToge1xyXG4gICAgICAnL2FwaSc6IHtcclxuICAgICAgICB0YXJnZXQ6IHByb2Nlc3MuZW52LlZJVEVfR0FURVdBWV9VUkwgfHwgJ2h0dHA6Ly9sb2NhbGhvc3Q6ODAwMicsXHJcbiAgICAgICAgY2hhbmdlT3JpZ2luOiB0cnVlLFxyXG4gICAgICB9LFxyXG4gICAgfSxcclxuICB9LFxyXG4gIHByZXZpZXc6IHtcclxuICAgIGhvc3Q6ICcwLjAuMC4wJyxcclxuICAgIGFsbG93ZWRIb3N0czogdHJ1ZSxcclxuICB9LFxyXG59KTtcclxuIl0sCiAgIm1hcHBpbmdzIjogIjtBQUErUixTQUFTLG9CQUFvQjtBQUM1VCxPQUFPLFdBQVc7QUFHbEIsSUFBTyxzQkFBUSxhQUFhO0FBQUEsRUFDMUIsU0FBUyxDQUFDLE1BQU0sQ0FBQztBQUFBLEVBQ2pCLFFBQVE7QUFBQSxJQUNOLE1BQU07QUFBQSxJQUNOLGNBQWM7QUFBQSxJQUNkLE1BQU07QUFBQSxJQUNOLFlBQVk7QUFBQSxJQUNaLE9BQU87QUFBQSxNQUNMLFFBQVE7QUFBQSxRQUNOLFFBQVEsUUFBUSxJQUFJLG9CQUFvQjtBQUFBLFFBQ3hDLGNBQWM7QUFBQSxNQUNoQjtBQUFBLElBQ0Y7QUFBQSxFQUNGO0FBQUEsRUFDQSxTQUFTO0FBQUEsSUFDUCxNQUFNO0FBQUEsSUFDTixjQUFjO0FBQUEsRUFDaEI7QUFDRixDQUFDOyIsCiAgIm5hbWVzIjogW10KfQo=
