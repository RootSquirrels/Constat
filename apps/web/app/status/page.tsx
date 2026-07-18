import Link from "next/link";
import { api, ApiError, type Status } from "@/lib/api";

export const dynamic = "force-dynamic";

function formatFreshness(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function freshnessColor(seconds: number | null): string {
  if (seconds === null) return "#6b7280"; // gray: never scanned
  if (seconds > 6 * 3600) return "#991b1b"; // red: > 6h (SLO breach)
  if (seconds > 3600) return "#92400e"; // amber: 1-6h
  return "#166534"; // green: < 1h
}

export default async function StatusPage() {
  let status: Status | null = null;
  let error: string | null = null;

  try {
    status = await api.getStatus();
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  if (error) {
    return (
      <main style={{ padding: "2rem", maxWidth: "56rem" }}>
        <h1 style={{ fontSize: "1.5rem", marginBottom: "1rem" }}>Status</h1>
        <div
          style={{
            padding: "1rem",
            border: "1px solid #fecaca",
            backgroundColor: "#fef2f2",
            color: "#991b1b",
            borderRadius: 8,
          }}
        >
          <strong>API error.</strong> {error}
          <p style={{ margin: "0.5rem 0 0 0", fontSize: "0.85rem" }}>
            Check that the API is running on <code>NEXT_PUBLIC_API_URL</code>.
          </p>
        </div>
      </main>
    );
  }

  if (!status) return null;

  const s = status;
  const sevTotal =
    s.insights_by_severity.critical +
    s.insights_by_severity.warning +
    s.insights_by_severity.info;

  return (
    <main style={{ padding: "2rem", maxWidth: "56rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.25rem" }}>Status</h1>
      <p style={{ color: "#555", marginBottom: "2rem" }}>
        One-glance view. Generated{" "}
        {new Date(s.generated_at).toLocaleString()}.
      </p>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
          gap: "1rem",
          marginBottom: "2rem",
        }}
      >
        <Card label="Accounts" value={s.accounts} />
        <Card
          label="Resources (active)"
          value={s.resources_active}
          sub={`${s.resources_total} total`}
        />
        <Card
          label="Insights"
          value={sevTotal}
          sub={`${s.insights_by_severity.critical} critical · ${s.insights_by_severity.warning} warning · ${s.insights_by_severity.info} info`}
          color={s.insights_by_severity.critical > 0 ? "#991b1b" : undefined}
        />
        <Card
          label="Inconclusive"
          value={s.inconclusive_total}
          sub="missing facts"
          color={s.inconclusive_total > 0 ? "#92400e" : undefined}
        />
        <Card
          label="Last source run"
          value={formatFreshness(s.source_run_freshness_seconds)}
          sub={s.last_source_run ? `${s.last_source_run.region} ${s.last_source_run.resource_type}` : "—"}
          color={freshnessColor(s.source_run_freshness_seconds)}
        />
      </section>

      <section style={{ marginBottom: "2rem" }}>
        <h2 style={{ fontSize: "1.1rem", marginBottom: "0.75rem" }}>
          Last insight rule execution
        </h2>
        {s.last_insight_run ? (
          <div
            style={{
              border: "1px solid #e5e7eb",
              borderRadius: 8,
              padding: "1rem",
              backgroundColor: "#fff",
            }}
          >
            <div style={{ display: "flex", gap: "0.75rem", alignItems: "baseline" }}>
              <code
                style={{
                  fontSize: "0.85rem",
                  color: "#6b7280",
                  backgroundColor: "#f3f4f6",
                  padding: "2px 8px",
                  borderRadius: 3,
                }}
              >
                {s.last_insight_run.rule_name}
              </code>
              <span
                style={{
                  fontSize: "0.75rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  color:
                    s.last_insight_run.status === "success"
                      ? "#166534"
                      : s.last_insight_run.status === "failed"
                        ? "#991b1b"
                        : "#92400e",
                }}
              >
                {s.last_insight_run.status}
              </span>
            </div>
            <p style={{ margin: "0.5rem 0 0 0", fontSize: "0.85rem", color: "#374151" }}>
              {s.last_insight_run.resources_scanned ?? 0} resources scanned ·{" "}
              {s.last_insight_run.insights_emitted ?? 0} insights emitted
            </p>
            <p style={{ margin: "0.25rem 0 0 0", fontSize: "0.75rem", color: "#6b7280" }}>
              Started{" "}
              {s.last_insight_run.started_at
                ? new Date(s.last_insight_run.started_at).toLocaleString()
                : "—"}
            </p>
            <Link
              href="/insight-runs"
              style={{ fontSize: "0.8rem", color: "#4b5563", display: "inline-block", marginTop: "0.5rem" }}
            >
              Full history →
            </Link>
          </div>
        ) : (
          <p style={{ color: "#6b7280" }}>
            No insight runs yet. Run{" "}
            <code>python -m constat_api.cli.run_insights --rule rds_eol</code> to start.
          </p>
        )}
      </section>

      <section style={{ fontSize: "0.85rem", color: "#6b7280" }}>
        <Link href="/insights" style={{ color: "#4b5563" }}>← back to insights</Link>
      </section>
    </main>
  );
}

function Card({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: number | string;
  sub?: string;
  color?: string;
}) {
  return (
    <div
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        padding: "1rem",
        backgroundColor: "#fff",
      }}
    >
      <div style={{ fontSize: "0.75rem", color: "#6b7280", textTransform: "uppercase", letterSpacing: "0.05em" }}>
        {label}
      </div>
      <div style={{ fontSize: "1.5rem", fontWeight: 600, color: color ?? "#111827", marginTop: "0.25rem" }}>
        {value}
      </div>
      {sub && (
        <div style={{ fontSize: "0.75rem", color: "#6b7280", marginTop: "0.25rem" }}>
          {sub}
        </div>
      )}
    </div>
  );
}
