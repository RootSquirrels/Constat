// API client for the Constat backend.
//
// Calls go through the Next.js proxy at /api/proxy/* (see
// app/api/proxy/[...path]/route.ts) which injects the X-API-Key
// server-side. The browser never sees the key. NEXT_PUBLIC_API_URL
// is kept for direct server-to-server calls (the proxy itself reads
// it from the server env, not from NEXT_PUBLIC_*).

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
// Every browser-side fetch goes through this prefix. The proxy
// strips it before forwarding to the backend.
const PROXY_PREFIX = "/api/proxy";

export type Severity = "info" | "warning" | "critical";

export type AckStatus =
  | "acknowledged"
  | "in_progress"
  | "resolved"
  | "dismissed";

export interface Insight {
  id: string;
  rule_name: string;
  resource_id: string | null;
  account_id: string | null;
  severity: Severity;
  title: string;
  payload: Record<string, unknown>;
  computed_at: string; // ISO 8601
  // P1 item 1: operator acknowledgment. NULL ack_status = "open"
  // (not yet triaged). PATCH /insights/{id} to set.
  ack_status: AckStatus | null;
  ack_at: string | null;
  ack_by: string | null;
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
  // "open" (virtual) | "acknowledged" | "in_progress" | "resolved" | "dismissed"
  ack_status?: "open" | AckStatus;
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
  // Browser-side calls go through the Next.js proxy. The proxy reads
  // CONSTAT_API_KEY from server env and adds X-API-Key. The key never
  // touches the client bundle.
  const url =
    typeof window === "undefined"
      ? `${API_URL}${path}` // server-side: direct to backend
      : `${PROXY_PREFIX}${path}`; // browser: through the proxy
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

  patchInsight: (id: string, ack_status: AckStatus, ack_by?: string) =>
    fetchJson<Insight>(`/insights/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ ack_status, ack_by: ack_by ?? null }),
    }),

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

export type ValueBasis = "ESTIMATED" | "ACTUAL";
export type MonetaryKind = "AVOIDABLE_SAVING" | "ACCOUNTING_DELTA";

// TS mirror of packages/core/src/constat_core/monetary.py — the single
// source of truth for monetary semantics (ADR-13). Do NOT edit this
// table without editing the Python registry: the pin test in
// tests/test_monetary_extraction.py fails CI when the two drift.
//
// kind matters for totals: an ACCOUNTING_DELTA (chargeback drift) is
// real money but NOT a saving the customer unlocks by acting — it must
// never be summed into a "savings" figure (client-committee finding).
const RULE_MONETARY: Record<
  string,
  { payloadKey: string; valueBasis: ValueBasis; kind: MonetaryKind }
> = {
  rds_eol: {
    payloadKey: "extended_support_monthly_usd",
    valueBasis: "ESTIMATED",
    kind: "AVOIDABLE_SAVING",
  },
  mysql_eol: {
    payloadKey: "extended_support_monthly_usd",
    valueBasis: "ESTIMATED",
    kind: "AVOIDABLE_SAVING",
  },
  aurora_eol: {
    payloadKey: "extended_support_monthly_usd",
    valueBasis: "ESTIMATED",
    kind: "AVOIDABLE_SAVING",
  },
  ebs_gp2_to_gp3: {
    payloadKey: "savings_monthly_usd",
    valueBasis: "ESTIMATED",
    kind: "AVOIDABLE_SAVING",
  },
  ebs_unattached: {
    payloadKey: "monthly_waste_usd",
    valueBasis: "ESTIMATED",
    kind: "AVOIDABLE_SAVING",
  },
  chargeback: {
    payloadKey: "drift_amortized_minus_billed_usd",
    valueBasis: "ACTUAL",
    kind: "ACCOUNTING_DELTA",
  },
};

export function insightMonthlyCostUsd(insight: Insight): number | null {
  const entry = RULE_MONETARY[insight.rule_name];
  if (!entry) return null;
  const raw = (insight.payload as Record<string, unknown>)[entry.payloadKey];
  return typeof raw === "number" ? raw : null;
}

export function insightValueBasis(insight: Insight): ValueBasis | "" {
  return RULE_MONETARY[insight.rule_name]?.valueBasis ?? "";
}

export function insightMonetaryKind(insight: Insight): MonetaryKind | null {
  return RULE_MONETARY[insight.rule_name]?.kind ?? null;
}

// Direct browser URL for the CSV export (no fetch — the browser downloads it).
// Goes through the proxy so the API key is injected server-side.
export function insightsCsvUrl(params: ListInsightsParams = {}): string {
  return `${PROXY_PREFIX}/insights/export.csv${buildQuery(params)}`;
}

export { ApiError, API_URL };
