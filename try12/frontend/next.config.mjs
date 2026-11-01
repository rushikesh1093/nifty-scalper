/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // The app uses no next/image. Unoptimized images skip the Image Optimization
  // API, the subject of several unpatched Next 14 advisories (docs/FRONTEND_AUDIT.md).
  images: { unoptimized: true },

  // `next dev` binds to 127.0.0.1 (package.json). Next 16 treats a page opened at
  // http://localhost:3000 as another origin for its dev resources, and the page
  // reloaded in a loop without ever fetching data. Both names are this machine.
  allowedDevOrigins: ["localhost", "127.0.0.1"],

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
