/** Backend origin injected at build time for local and hosted deployments. */
export const BACKEND_URL = (
  import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000'
).replace(/\/$/, '');
