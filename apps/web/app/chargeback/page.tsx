import { api, ApiError, type Insight } from "@/lib/api";

export const dynamic = "force-dynamic";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// FOCUS coverage diagnostics (GET /focus/coverage). Fetched inline here —
// not via lib/api.ts — so the banner stays local to this page.
interface FocusCoverageAccount {
  account_id: string;
  periods: [string, string][];
  covered_months: number;
  missing_months: string[];
  stale: boolean;
  first_period: string | null;
  last_period: string | null;
}

interface FocusCoverage {
  accounts: FocusCoverageAccount[];
  has_gaps: boolean;
  has_stale: boolean;
}

// Coverage is best-effort: if the fetch fails we render no banner rather
// than taking the chargeback page down with it.
async function fetchFocusCoverage(): Promise<FocusCoverage | null> {
  try {
    const res = await fetch(`${API_URL}/focus/coverage`, { cache: "no-store" });
    if (!res.ok) return null;
    return (await res.json()) as FocusCoverage;
  } catch {
    return null;
  }
}

function groupByAccount(insights: Insight[]): Record<string, Insight[]> {
  const groups: Record<string, Insight[]> = {};
  for (const i of insights) {
    const key = i.account_id ?? "unknown";
    if (!groups[key]) groups[key] = [];
    groups[key].push(i);
  }
  return groups;
}

function fmtUsd(n: number | undefined): string {
  if (n === undefined) return "—";
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function fmtPeriod(p: Insight): string {
  const payload = p.payload as Record<string, unknown>;
  const start = payload.period_start;
  const end = payload.period_end;
  if (typeof start === "string" && typeof end === "string") {
    return `${start} → ${end}`;
  }
  return (payload.period_label as string) ?? "all-time";
}

export default async function ChargebackPage() {
  let insights: Insight[] = [];
  let error: string | null = null;

  try {
    insights = await api.listChargeback(100);
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  const coverage = await fetchFocusCoverage();
  const gapAccounts =
    coverage?.accounts.filter((a) => a.missing_months.length > 0) ?? [];
  const staleAccounts = coverage?.accounts.filter((a) => a.stale) ?? [];
  const showCoverageBanner =
    coverage !== null && (coverage.has_gaps || coverage.has_stale);

  const groups = groupByAccount(insights);
  const total = insights.length;
  const accountIds = Object.keys(groups).sort();

  return (
    <main style={{ padding: "2rem", maxWidth: "56rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.25rem" }}>Chargeback</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        Per-account × service cost drift (amortized vs billed). One row per
        billing period. Sourced from FOCUS 1.0.
      </p>

      {error && (
        <div
          style={{
            padding: "1rem",
            border: "1px solid #fecaca",
            backgroundColor: "#fef2f2",
            color: "#991b1b",
            borderRadius: 8,
            marginBottom: "1rem",
          }}
        >
          <strong>API error.</strong> {error}
        </div>
      )}

      {showCoverageBanner && (
        <div
          style={{
            padding: "1rem",
            border: "1px solid #fcd34d",
            backgroundColor: "#fffbeb",
            color: "#92400e",
            borderRadius: 8,
            marginBottom: "1rem",
          }}
        >
          <strong>
            FOCUS coverage incomplete — chargeback may be understated.
          </strong>
          <ul style={{ margin: "0.5rem 0 0", paddingLeft: "1.25rem" }}>
            {gapAccounts.map((a) => (
              <li key={`gap-${a.account_id}`}>
                account <code>{a.account_id}</code> is missing{" "}
                {a.missing_months.join(", ")}
              </li>
            ))}
            {staleAccounts.map((a) => (
              <li key={`stale-${a.account_id}`}>
                account <code>{a.account_id}</code>: data older than 45 days
                (last period ended {a.last_period})
              </li>
            ))}
          </ul>
        </div>
      )}

      {total === 0 && !error && (
        <section
          style={{
            marginTop: "2rem",
            padding: "1.5rem",
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            backgroundColor: "#fff",
          }}
        >
          <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>To populate data</h2>
          <ol style={{ marginBottom: 0, lineHeight: 1.8 }}>
            <li>
              Export a FOCUS 1.0 CSV from your AWS account (CUR with FOCUS
              columns).
            </li>
            <li>
              Run the ingestion CLI:
              <pre
                style={{
                  backgroundColor: "#f3f4f6",
                  padding: "0.75rem",
                  borderRadius: 4,
                  margin: "0.5rem 0",
                  fontSize: "0.85rem",
                }}
              >
                python -m constat_api.cli.focus --account 111111111111 --csv focus.csv
              </pre>
            </li>
            <li>
              Run the chargeback rule:
              <pre
                style={{
                  backgroundColor: "#f3f4f6",
                  padding: "0.75rem",
                  borderRadius: 4,
                  margin: "0.5rem 0",
                  fontSize: "0.85rem",
                }}
              >
                python -m constat_api.cli.run_insights --rule chargeback
              </pre>
            </li>
          </ol>
        </section>
      )}

      {accountIds.map((accountId) => {
        const rows = groups[accountId];
        const totalBilled = rows.reduce(
          (acc, r) => acc + ((r.payload.billed_cost_usd as number) ?? 0),
          0,
        );
        const totalAmortized = rows.reduce(
          (acc, r) => acc + ((r.payload.amortized_cost_usd as number) ?? 0),
          0,
        );
        const drift = totalAmortized - totalBilled;
        return (
          <section key={accountId} style={{ marginBottom: "2rem" }}>
            <h2
              style={{
                fontSize: "1.1rem",
                marginBottom: "0.5rem",
                color: "#374151",
              }}
            >
              Account <code style={{ fontSize: "0.9rem" }}>{accountId}</code>
            </h2>
            <p style={{ color: "#6b7280", fontSize: "0.9rem", marginBottom: "0.75rem" }}>
              {rows.length} insight{rows.length === 1 ? "" : "s"} ·{" "}
              billed {fmtUsd(totalBilled)} · amortized {fmtUsd(totalAmortized)} ·{" "}
              drift{" "}
              <span
                style={{
                  color: drift > 0 ? "#991b1b" : "#065f46",
                  fontWeight: 600,
                }}
              >
                {drift > 0 ? "+" : ""}
                {fmtUsd(drift)}
              </span>
            </p>
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontSize: "0.9rem",
                backgroundColor: "#fff",
                border: "1px solid #e5e7eb",
                borderRadius: 8,
                overflow: "hidden",
              }}
            >
              <thead>
                <tr style={{ backgroundColor: "#f9fafb", textAlign: "left" }}>
                  <th style={{ padding: "0.5rem 0.75rem" }}>Period</th>
                  <th style={{ padding: "0.5rem 0.75rem" }}>Service</th>
                  <th style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>Billed</th>
                  <th style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>Amortized</th>
                  <th style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>Drift</th>
                  <th style={{ padding: "0.5rem 0.75rem" }}>Severity</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const payload = row.payload as Record<string, unknown>;
                  const billed = (payload.billed_cost_usd as number) ?? 0;
                  const amortized = (payload.amortized_cost_usd as number) ?? 0;
                  const rowDrift = amortized - billed;
                  return (
                    <tr key={row.id} style={{ borderTop: "1px solid #f3f4f6" }}>
                      <td style={{ padding: "0.5rem 0.75rem" }}>{fmtPeriod(row)}</td>
                      <td style={{ padding: "0.5rem 0.75rem" }}>{String(payload.service ?? "—")}</td>
                      <td style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                        {fmtUsd(billed)}
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                        {fmtUsd(amortized)}
                      </td>
                      <td
                        style={{
                          padding: "0.5rem 0.75rem",
                          textAlign: "right",
                          color: rowDrift > 0 ? "#991b1b" : "#065f46",
                          fontWeight: 600,
                        }}
                      >
                        {rowDrift > 0 ? "+" : ""}
                        {fmtUsd(rowDrift)}
                      </td>
                      <td style={{ padding: "0.5rem 0.75rem" }}>
                        <span
                          style={{
                            textTransform: "uppercase",
                            fontSize: "0.7rem",
                            fontWeight: 600,
                            color:
                              row.severity === "critical"
                                ? "#991b1b"
                                : row.severity === "warning"
                                  ? "#92400e"
                                  : "#1e40af",
                          }}
                        >
                          {row.severity}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        );
      })}
    </main>
  );
}
