import Link from "next/link";
import {
  api,
  ApiError,
  insightsCsvUrl,
  type Insight,
  type Inconclusive,
  type Severity,
} from "@/lib/api";
import InsightCard from "@/components/InsightCard";

export const dynamic = "force-dynamic"; // never cache; always fetch fresh

const SEVERITY_ORDER: Severity[] = ["critical", "warning", "info"];

function groupBySeverity(insights: Insight[]): Record<Severity, Insight[]> {
  const groups: Record<Severity, Insight[]> = { critical: [], warning: [], info: [] };
  for (const i of insights) groups[i.severity].push(i);
  return groups;
}

export default async function InsightsPage() {
  let insights: Insight[] = [];
  let inconclusive: Inconclusive[] = [];
  let error: string | null = null;
  let incompError: string | null = null;

  try {
    insights = await api.listInsights({ limit: 200 });
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  // Fetch inconclusive in parallel (best-effort: don't fail the page if this errors).
  try {
    inconclusive = await api.listInconclusive({ limit: 50 });
  } catch (e) {
    incompError = e instanceof ApiError ? `API ${e.status}` : String(e);
  }

  const groups = groupBySeverity(insights);
  const total = insights.length;
  const incTotal = inconclusive.length;
  const sevWithCount = SEVERITY_ORDER.filter((s) => groups[s].length).length;

  return (
    <main style={{ padding: "2rem", maxWidth: "56rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.25rem" }}>Insights</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        {total === 0
          ? "No insights yet."
          : `${total} insight${total === 1 ? "" : "s"} across ${sevWithCount} severity level${sevWithCount === 1 ? "" : "s"}.`}
        {" · "}
        <a href={insightsCsvUrl({ limit: 500 })} style={{ color: "#4b5563" }}>
          Export CSV
        </a>
      </p>

      {incTotal > 0 && (
        <div
          style={{
            padding: "0.75rem 1rem",
            border: "1px solid #fde68a",
            backgroundColor: "#fffbeb",
            color: "#92400e",
            borderRadius: 8,
            marginBottom: "1.5rem",
            display: "flex",
            gap: "0.75rem",
            alignItems: "center",
          }}
        >
          <span style={{ fontWeight: 600 }}>{incTotal}</span>
          <span>inconclusive record{incTotal === 1 ? "" : "s"}</span>
          <span style={{ color: "#6b7280" }}>
            — rule could not conclude (missing facts or scope not proven)
          </span>
          <Link
            href="/inconclusives"
            style={{ marginLeft: "auto", color: "#92400e", fontWeight: 500 }}
          >
            view all →
          </Link>
        </div>
      )}
      {incompError && (
        <p style={{ color: "#9ca3af", fontSize: "0.8rem" }}>
          (inconclusive count unavailable: {incompError})
        </p>
      )}

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
          <p style={{ margin: "0.5rem 0 0 0", fontSize: "0.85rem" }}>
            Check that the API is running on <code>NEXT_PUBLIC_API_URL</code> (default
            <code> http://localhost:8000</code>) and that the database has been seeded with
            insights.
          </p>
        </div>
      )}

      {SEVERITY_ORDER.map((severity) =>
        groups[severity].length === 0 ? null : (
          <section key={severity} style={{ marginBottom: "2rem" }}>
            <h2
              style={{
                fontSize: "1.1rem",
                marginBottom: "0.75rem",
                color: "#374151",
              }}
            >
              {severity.charAt(0).toUpperCase() + severity.slice(1)}{" "}
              <span style={{ color: "#9ca3af", fontWeight: 400 }}>
                ({groups[severity].length})
              </span>
            </h2>
            {groups[severity].map((i) => (
              <InsightCard key={i.id} insight={i} />
            ))}
          </section>
        ),
      )}
    </main>
  );
}
