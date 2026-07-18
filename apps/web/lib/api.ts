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

export interface InsightRun {
  id: string;
  rule_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  resources_scanned: number | null;
  insights_emitted: number | null;
  error: string | null;
}

export interface Account {
  id: string;
  external_id: string;
  name: string | null;
  created_at: string;
}

export interface Status {
  generated_at: string;
  accounts: number;
  resources_total: number;
  resources_active: number;
  insights_total: number;
  insights_by_severity: {
    critical: number;
    warning: number;
    info: number;
  };
  inconclusive_total: number;
  last_insight_run: InsightRun | null;
  last_source_run: {
    account_external_id: string | null;
    region: string;
    resource_type: string;
    finished_at: string | null;
    status: string;
    resources_found: number | null;
  } | null;
  source_run_freshness_seconds: number | null;
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

  runInsights: (rule = "rds_eol", periodLabel = "all-time") =>
    fetchJson<RunInsightsResult>(`/insights/run`, {
      method: "POST",
      body: JSON.stringify({ rule, period_label: periodLabel }),
    }),

  // Chargeback: listInsights filtered to rule_name=chargeback is the
  // current pattern. The dedicated /chargeback endpoint is V2.
  listChargeback: (limit = 100) =>
    fetchJson<Insight[]>(`/insights?rule_name=chargeback&limit=${limit}`),

  listInsightRuns: (params: { rule_name?: string; limit?: number } = {}) =>
    fetchJson<InsightRun[]>(`/insight-runs${buildQuery(params)}`),

  getStatus: () => fetchJson<Status>("/status"),

  listAccounts: (params: { limit?: number } = {}) =>
    fetchJson<Account[]>(`/accounts${buildQuery(params)}`),
};

export { ApiError, API_URL };
