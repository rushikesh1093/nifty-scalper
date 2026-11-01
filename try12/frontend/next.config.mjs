/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // The app uses no next/image. Unoptimized images skip the Image Optimization
  // API, the subject of several unpatched Next 14 advisories (docs/FRONTEND_AUDIT.md).
  images: { unoptimized: true },

  // Expose BACKEND_INTERNAL_URL to server-side Next.js code (SSR, Route Handlers,
  // Server Components).  This resolves to the Docker internal service name when
  // running in a container, and falls back to localhost for local development.
  // Usage in server code:
  //   const apiBase = process.env.BACKEND_INTERNAL_URL ?? process.env.NEXT_PUBLIC_API_URL;
  env: {
    BACKEND_INTERNAL_URL: process.env.BACKEND_INTERNAL_URL ?? "http://localhost:8000/api/v1",
  },
};

export default nextConfig;
