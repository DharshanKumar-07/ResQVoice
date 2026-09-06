/**
 * Backend origin injected at build time. Production defaults to the stable
 * Render service so an omitted Vercel environment variable can never send a
 * deployed browser to localhost. Local development may override it with
 * VITE_BACKEND_URL=http://127.0.0.1:8000.
 */
export const BACKEND_URL = (
  import.meta.env.VITE_BACKEND_URL ?? 'https://resqvoice-api-p1cj.onrender.com'
).replace(/\/$/, '');
