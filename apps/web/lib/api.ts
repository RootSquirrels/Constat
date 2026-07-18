// API client for the Constat backend.
// Server-side fetch (called from React Server Components). NEXT_PUBLIC_API_URL
// is read at build time; default points at the dev API on :8000.

const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type Severity = "info" | "warning" | "critical";

export interface Insight {
  id: string;
  rule_name: string;
  resource_id: string | null;
  account_id: string | null;
  severity: Severity;
  title: string;
  payload: Record<string, unknown>;
  computed_at: string; // ISO 8601
}

export interface Inconclusive {
  id: string;
  rule_name: string;
  resource_id: string | null;
  account_id: string | null;
  missing_facts: string[];
  reason: string | null;
  computed_at: string;
}

export interface HealthResponse {
  status: string;
}

export interface ListInsightsParams {
  rule_name?: string;
  severity?: Severity;
  account_id?: string;
  limit?: number;
  offset?: number;
}

export interface ListInconclusiveParams {
  rule_name?: string;
  account_id?: string;
  limit?: number;
  offset?: number;
}

export interface RunInsightsResult {
  rule_name: string;
  resources_scanned: number;
  insights_emitted: number;
  inconclusive_emitted: number;
  errors: string[];
}

class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly path: string,
    public readonly body: string,
  ) {
    super(`API ${path} returned ${status}: ${body}`);
  }
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_URL}${path}`;
  const res = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text();
    throw new ApiError(res.status, path, body);
  }
  return (await res.json()) as T;
}

function buildQuery(params: object): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

export const api = {
  health: () => fetchJson<HealthResponse>("/health"),

  listInsights: (params: ListInsightsParams = {}) =>
    fetchJson<Insight[]>(`/insights${buildQuery(params)}`),

  getInsight: (id: string) => fetchJson<Insight>(`/insights/${id}`),

  listInconclusive: (params: ListInconclusiveParams = {}) =>
    fetchJson<Inconclusive[]>(`/inconclusives${buildQuery(params)}`),

  runInsights: (rule = "rds_eol") =>
    fetchJson<RunInsightsResult>(`/insights/run`, {
      method: "POST",
      body: JSON.stringify({ rule }),
    }),
};

export { ApiError, API_URL };
