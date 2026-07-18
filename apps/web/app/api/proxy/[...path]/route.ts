// Server-side proxy: forward all /api/proxy/* requests to the backend,
// adding the X-API-Key header server-side. The key is read from
// CONSTAT_API_KEY at request time (not bundled into the JS), so the
// browser never sees it. The frontend calls `/api/proxy/...` instead of
// the backend URL directly.
//
// Why proxy and not "send the key from the browser":
// - Secrets must not live in browser-accessible code. A NEXT_PUBLIC_*
//   env var would land in the JS bundle and leak the key.
// - Single chokepoint for auth header injection, error normalization,
//   audit logging, and rate limiting.
//
// Trade-off: every request from the browser pays one extra hop. For
// V1's traffic (operator dashboard, ~10 req/s) this is invisible.

import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.CONSTAT_BACKEND_URL ?? "http://localhost:8000";
const API_KEY = process.env.CONSTAT_API_KEY;

const ALLOWED_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"];
const ALLOWED_PREFIXES = [
  "/health",
  "/insights",
  "/inconclusives",
  "/insight-runs",
  "/accounts",
  "/status",
  "/chargeback",
  "/admin",
  "/collect",
  "/retention",
];

// Runtime-only env. The handler is server-only, so secrets are safe
// to read here. They are NOT exposed to the client bundle.
export const runtime = "nodejs";
// Force dynamic because the handler reads env + uses request body.
export const dynamic = "force-dynamic";

async function handle(req: NextRequest, path: string[]) {
  if (!API_KEY) {
    // Fail loud at request time, not at build time. A misconfigured
    // deploy (forgot to set CONSTAT_API_KEY) returns 503 to the
    // browser, not 401-from-the-backend which looks like an auth bug.
    return NextResponse.json(
      {
        error: "proxy_misconfigured",
        detail:
          "CONSTAT_API_KEY is unset on the web server. Set it in the deploy env. " +
          "The browser never has the key; this proxy injects it server-side.",
      },
      { status: 503 },
    );
  }

  const pathStr = (path ?? []).join("/");
  if (!ALLOWED_PREFIXES.some((p) => pathStr === p || pathStr.startsWith(p + "/"))) {
    return NextResponse.json(
      { error: "proxy_path_not_allowed", path: pathStr },
      { status: 404 },
    );
  }

  const method = req.method.toUpperCase();
  if (!ALLOWED_METHODS.includes(method)) {
    return NextResponse.json(
      { error: "method_not_allowed", method },
      { status: 405 },
    );
  }

  // Forward query string verbatim. Next.js gives it parsed; we
  // re-serialize so the backend sees exactly what the browser sent.
  const url = new URL(req.nextUrl.search, `${BACKEND_URL}/${pathStr}`);

  // Stream the body for non-GET. The browser is the only caller, so
  // we can trust the content-type (and limit accepted types to JSON
  // or octet-stream for safety).
  let body: BodyInit | undefined;
  if (method !== "GET" && method !== "HEAD") {
    const contentType = req.headers.get("content-type") ?? "";
    if (
      contentType.includes("application/json") ||
      contentType.includes("application/octet-stream") ||
      contentType === ""
    ) {
      body = await req.arrayBuffer();
    } else {
      return NextResponse.json(
        { error: "proxy_content_type_not_allowed", contentType },
        { status: 415 },
      );
    }
  }

  // Build the upstream request. Forward only the headers the backend
  // needs; never forward the browser's cookies or auth (the browser
  // doesn't have any — that's the whole point).
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("X-API-Key", API_KEY);
  if (body !== undefined) {
    upstreamHeaders.set(
      "Content-Type",
      req.headers.get("content-type") ?? "application/json",
    );
  }
  // Forward request_id if the page set one (for cross-service log
  // correlation). The backend already reads x-request-id.
  const reqId = req.headers.get("x-request-id");
  if (reqId) upstreamHeaders.set("x-request-id", reqId);

  const upstreamRes = await fetch(url, {
    method,
    headers: upstreamHeaders,
    body,
    cache: "no-store",
  });

  // Pass through the response. We don't try to interpret it — the
  // backend's status + body are the contract.
  const responseHeaders = new Headers();
  const contentType = upstreamRes.headers.get("content-type");
  if (contentType) responseHeaders.set("content-type", contentType);
  // The backend already returns JSON for errors; just pass it through.
  const responseBody = await upstreamRes.arrayBuffer();

  return new NextResponse(responseBody, {
    status: upstreamRes.status,
    headers: responseHeaders,
  });
}

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params;
  return handle(req, path);
}
export async function POST(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params;
  return handle(req, path);
}
export async function PATCH(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params;
  return handle(req, path);
}
export async function PUT(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params;
  return handle(req, path);
}
export async function DELETE(
  req: NextRequest,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path } = await ctx.params;
  return handle(req, path);
}
export async function OPTIONS() {
  // CORS preflight. The browser never sends the key, so the preflight
  // also doesn't need it.
  return new NextResponse(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, x-request-id",
      "Access-Control-Max-Age": "600",
    },
  });
}
