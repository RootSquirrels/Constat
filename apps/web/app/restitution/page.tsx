import {
  api,
  ApiError,
  insightMonthlyCostUsd,
  insightValueBasis,
  type Account,
  type Inconclusive,
  type Insight,
  type Status,
} from "@/lib/api";

export const dynamic = "force-dynamic";

// Print stylesheet: clean A4, no nav chrome. A plain global <style> tag
// scoped to this page (the app otherwise uses inline styles, no CSS files).
const printCss = `
@media print {
  nav { display: none !important; }
  body { background-color: #fff !important; }
  main { max-width: none !important; padding: 0 !important; }
  section, table { page-break-inside: avoid; }
  a { color: inherit !important; text-decoration: none !important; }
}
`;

function fmtUsd(n: number | null): string {
  if (n === null) return "—";
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
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

function countByReason(items: Inconclusive[]): [string, number][] {
  const counts: Record<string, number> = {};
  for (const i of items) {
    const key = i.reason ?? "unknown";
    counts[key] = (counts[key] ?? 0) + 1;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1]);
}

export default async function RestitutionPage() {
  let insights: Insight[] = [];
  let inconclusive: Inconclusive[] = [];
  let chargeback: Insight[] = [];
  let accounts: Account[] = [];
  let status: Status | null = null;
  let error: string | null = null;

  // Best-effort per section: one failing endpoint must not blank the
  // document we leave behind with the prospect.
  try {
    insights = await api.listInsights({ limit: 500 });
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }
  try {
    [inconclusive, chargeback, accounts, status] = await Promise.all([
      api.listInconclusive({ limit: 500 }),
      api.listChargeback(500),
      api.listAccounts(),
      api.getStatus(),
    ]);
  } catch {
    // counts/totals degrade to empty sections below
  }

  const generatedAt = new Date();
  const reasonCounts = countByReason(inconclusive);
  const chargebackByAccount = groupByAccount(chargeback);
  const totalMonthly = insights.reduce(
    (acc, i) => acc + (insightMonthlyCostUsd(i) ?? 0),
    0,
  );
  const lastRun = status?.last_source_run ?? null;

  return (
    <main style={{ padding: "2rem", maxWidth: "56rem" }}>
      <style>{printCss}</style>

      <header style={{ marginBottom: "2rem" }}>
        <h1 style={{ fontSize: "1.75rem", marginBottom: "0.25rem" }}>
          Constat — POC Restitution
        </h1>
        <p style={{ color: "#555", margin: 0 }}>
          Scope:{" "}
          {accounts.length === 0
            ? "pilot accounts"
            : accounts
                .map((a) => a.name ?? a.external_id)
                .join(", ")}{" "}
          · generated {generatedAt.toLocaleDateString("en-US", {
            year: "numeric",
            month: "long",
            day: "numeric",
          })}
        </p>
      </header>

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

      <section style={{ marginBottom: "2rem" }}>
        <h2 style={{ fontSize: "1.1rem", marginBottom: "0.75rem" }}>
          Proven gaps ({insights.length})
        </h2>
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontSize: "0.9rem",
            backgroundColor: "#fff",
            border: "1px solid #e5e7eb",
          }}
        >
          <thead>
            <tr style={{ backgroundColor: "#f9fafb", textAlign: "left" }}>
              <th style={{ padding: "0.5rem 0.75rem" }}>Insight</th>
              <th style={{ padding: "0.5rem 0.75rem" }}>Severity</th>
              <th style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                Cost / month
              </th>
              <th style={{ padding: "0.5rem 0.75rem" }}>Basis</th>
            </tr>
          </thead>
          <tbody>
            {insights.map((i) => (
              <tr key={i.id} style={{ borderTop: "1px solid #f3f4f6" }}>
                <td style={{ padding: "0.5rem 0.75rem" }}>{i.title}</td>
                <td
                  style={{
                    padding: "0.5rem 0.75rem",
                    textTransform: "uppercase",
                    fontSize: "0.7rem",
                    fontWeight: 600,
                    color:
                      i.severity === "critical"
                        ? "#991b1b"
                        : i.severity === "warning"
                          ? "#92400e"
                          : "#1e40af",
                  }}
                >
                  {i.severity}
                </td>
                <td style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                  {fmtUsd(insightMonthlyCostUsd(i))}
                </td>
                <td style={{ padding: "0.5rem 0.75rem", fontSize: "0.8rem" }}>
                  {insightValueBasis(i)}
                </td>
              </tr>
            ))}
            <tr
              style={{
                borderTop: "2px solid #e5e7eb",
                fontWeight: 600,
              }}
            >
              <td style={{ padding: "0.5rem 0.75rem" }} colSpan={2}>
                Total (known costs)
              </td>
              <td style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                {fmtUsd(totalMonthly)}
              </td>
              <td style={{ padding: "0.5rem 0.75rem" }} />
            </tr>
          </tbody>
        </table>
      </section>

      <section style={{ marginBottom: "2rem" }}>
        <h2 style={{ fontSize: "1.1rem", marginBottom: "0.5rem" }}>
          What we don&apos;t know ({inconclusive.length})
        </h2>
        <p style={{ color: "#555", fontSize: "0.9rem", marginBottom: "0.75rem" }}>
          Records where the rules could not conclude — missing facts or scope
          not yet proven. Tools that silently omit these give you false
          confidence; we list them.
        </p>
        {reasonCounts.length > 0 && (
          <ul style={{ margin: 0, lineHeight: 1.8, fontSize: "0.9rem" }}>
            {reasonCounts.map(([reason, count]) => (
              <li key={reason}>
                <strong>{count}</strong>{" "}
                <span style={{ textTransform: "capitalize" }}>
                  {reason.replace(/_/g, " ")}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section style={{ marginBottom: "2rem" }}>
        <h2 style={{ fontSize: "1.1rem", marginBottom: "0.75rem" }}>
          Chargeback summary
        </h2>
        {Object.keys(chargebackByAccount).length === 0 ? (
          <p style={{ color: "#6b7280", fontSize: "0.9rem" }}>
            No chargeback data (FOCUS ingestion not run in this scope).
          </p>
        ) : (
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: "0.9rem",
              backgroundColor: "#fff",
              border: "1px solid #e5e7eb",
            }}
          >
            <thead>
              <tr style={{ backgroundColor: "#f9fafb", textAlign: "left" }}>
                <th style={{ padding: "0.5rem 0.75rem" }}>Account</th>
                <th style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                  Billed
                </th>
                <th style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                  Amortized
                </th>
                <th style={{ padding: "0.5rem 0.75rem", textAlign: "right" }}>
                  Drift
                </th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(chargebackByAccount)
                .sort()
                .map((accountId) => {
                  const rows = chargebackByAccount[accountId];
                  const billed = rows.reduce(
                    (acc, r) =>
                      acc + ((r.payload.billed_cost_usd as number) ?? 0),
                    0,
                  );
                  const amortized = rows.reduce(
                    (acc, r) =>
                      acc + ((r.payload.amortized_cost_usd as number) ?? 0),
                    0,
                  );
                  const drift = amortized - billed;
                  return (
                    <tr
                      key={accountId}
                      style={{ borderTop: "1px solid #f3f4f6" }}
                    >
                      <td
                        style={{
                          padding: "0.5rem 0.75rem",
                          fontFamily: "monospace",
                          fontSize: "0.8rem",
                        }}
                      >
                        {accountId}
                      </td>
                      <td
                        style={{
                          padding: "0.5rem 0.75rem",
                          textAlign: "right",
                        }}
                      >
                        {fmtUsd(billed)}
                      </td>
                      <td
                        style={{
                          padding: "0.5rem 0.75rem",
                          textAlign: "right",
                        }}
                      >
                        {fmtUsd(amortized)}
                      </td>
                      <td
                        style={{
                          padding: "0.5rem 0.75rem",
                          textAlign: "right",
                          color: drift > 0 ? "#991b1b" : "#065f46",
                          fontWeight: 600,
                        }}
                      >
                        {drift > 0 ? "+" : ""}
                        {fmtUsd(drift)}
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        )}
      </section>

      <footer
        style={{
          borderTop: "1px solid #e5e7eb",
          paddingTop: "1rem",
          color: "#6b7280",
          fontSize: "0.85rem",
        }}
      >
        <p style={{ margin: 0 }}>
          Pilot scope: {accounts.length} account
          {accounts.length === 1 ? "" : "s"}, rules rds_eol + chargeback.
          {lastRun
            ? ` Every figure above is backed by a recorded source_run — most recently ${lastRun.resource_type} in ${lastRun.region} (account ${lastRun.account_external_id ?? "unknown"}), finished ${lastRun.finished_at ? new Date(lastRun.finished_at).toLocaleString() : "—"} with status ${lastRun.status}.`
            : " No source_run recorded yet — figures will appear after the first scan."}
        </p>
      </footer>
    </main>
  );
}
