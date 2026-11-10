/**
 * python-backend.ts — single source of truth for the Python FastAPI base URL.
 *
 * The Node API (8002) is the front gateway; any /api/* it doesn't own is
 * proxied to the Python backend (default :8000) by proxy-fallback.ts. A few
 * Node routers (e.g. groups-chat.ts, which intercepts chat send to run replies
 * through the Node dispatcher) also need to call Python directly for
 * persistence. Both import this constant so the target is defined once.
 */
export const PYTHON_BACKEND_URL =
  process.env.PYTHON_BACKEND_URL ?? 'http://localhost:8000';
