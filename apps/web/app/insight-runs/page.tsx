import Link from "next/link";
import { api, ApiError, type InsightRun } from "@/lib/api";

export const dynamic = "force-dynamic";

function statusColor(status: string): string {
  if (status === "success") return "#166534";
  if (status === "failed") return "#991b1b";
  if (status === "partial") return "#92400e";
  if (status === "running") return "#1e40af";
  return "#6b7280";
}

function formatDuration(start: string, end: string | null): string {
  if (!end) return "—";
  const s = new Date(start).getTime();
  const e = new Date(end).getTime();
  if (Number.isNaN(s) || Number.isNaN(e)) return "—";
  const sec = Math.max(0, Math.floor((e - s) / 1000));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

export default async function InsightRunsPage() {
  let runs: InsightRun[] = [];
  let error: string | null = null;

  try {
    runs = await api.listInsightRuns({ limit: 100 });
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  return (
    <main style={{ padding: "2rem", maxWidth: "64rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.25rem" }}>Insight runs</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        Audit history of insight rule executions. Newest first. Useful for
        "when did the last rds_eol run?" and "what did it emit?".
        <br />
        <Link href="/insights" style={{ color: "#4b5563", fontSize: "0.85rem" }}>
          ← back to insights
        </Link>
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

      {runs.length === 0 ? (
        <p style={{ color: "#6b7280" }}>
          No runs yet. Trigger one with{" "}
          <code>python -m constat_api.cli.run_insights --rule rds_eol</code>.
        </p>
      ) : (
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
          <thead style={{ backgroundColor: "#f9fafb" }}>
            <tr>
              <th style={th}>Rule</th>
              <th style={th}>Status</th>
              <th style={th}>Started</th>
              <th style={th}>Duration</th>
              <th style={{ ...th, textAlign: "right" }}>Scanned</th>
              <th style={{ ...th, textAlign: "right" }}>Insights</th>
              <th style={th}>Error</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id} style={{ borderTop: "1px solid #f3f4f6" }}>
                <td style={td}>
                  <code
                    style={{
                      fontSize: "0.8rem",
                      color: "#6b7280",
                      backgroundColor: "#f3f4f6",
                      padding: "1px 6px",
                      borderRadius: 3,
                    }}
                  >
                    {r.rule_name}
                  </code>
                </td>
                <td style={{ ...td, color: statusColor(r.status), fontWeight: 600, textTransform: "uppercase", fontSize: "0.75rem" }}>
                  {r.status}
                </td>
                <td style={td}>
                  {r.started_at ? new Date(r.started_at).toLocaleString() : "—"}
                </td>
                <td style={td}>{formatDuration(r.started_at, r.finished_at)}</td>
                <td style={{ ...td, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                  {r.resources_scanned ?? "—"}
                </td>
                <td style={{ ...td, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                  {r.insights_emitted ?? "—"}
                </td>
                <td
                  style={{
                    ...td,
                    color: r.error ? "#991b1b" : "#6b7280",
                    maxWidth: 200,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    fontSize: "0.8rem",
                  }}
                  title={r.error ?? undefined}
                >
                  {r.error ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}

const th: React.CSSProperties = {
  textAlign: "left",
  padding: "0.5rem 0.75rem",
  fontSize: "0.75rem",
  color: "#6b7280",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  fontWeight: 600,
};

const td: React.CSSProperties = {
  padding: "0.5rem 0.75rem",
};
