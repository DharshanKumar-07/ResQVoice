/**
 * Backend origin injected at build time. Production defaults to the stable
 * Render service so an omitted Vercel environment variable can never send a
 * deployed browser to localhost. Local development may override it with
 * VITE_BACKEND_URL=http://127.0.0.1:8000.
 */
const configuredBackendUrl = import.meta.env.VITE_BACKEND_URL;
// The initial Vercel build was configured with this retired service origin.
// Preserve a safe migration path while the dashboard variable is updated.
const LEGACY_RENDER_BACKEND_URL = 'https://resqvoice-api-p1cj.onrender.com';

export const BACKEND_URL = (
  configuredBackendUrl === LEGACY_RENDER_BACKEND_URL
    ? 'https://resqvoice-api.onrender.com'
    : configuredBackendUrl ?? 'https://resqvoice-api.onrender.com'
).replace(/\/$/, '');
