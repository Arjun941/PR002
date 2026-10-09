// The FastAPI backend serves /api; proxy it so the browser stays same-origin.
const backend = process.env.BACKEND_URL || "http://localhost:8000";

/** @type {import('next').NextConfig} */
export default {
  // The proxy gives up after 30 s by default, which cuts off script drafting (one model call writing every
  // language) with a bare 500. Match the backend's longest model wait (Ollama, 300 s).
  experimental: { proxyTimeout: 300_000 },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};
