import { notFound } from "next/navigation";
import Link from "next/link";
import { api, ApiError, type Insight } from "@/lib/api";
import SeverityBadge from "@/components/SeverityBadge";

export const dynamic = "force-dynamic";

export default async function InsightDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let insight: Insight | null = null;
  let error: string | null = null;

  try {
    insight = await api.getInsight(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      notFound();
    }
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  if (error) {
    return (
      <main style={{ padding: "2rem", maxWidth: "48rem" }}>
        <Link href="/insights" style={{ color: "#4b5563" }}>
          ← Back to insights
        </Link>
        <h1 style={{ fontSize: "1.5rem", marginTop: "1rem" }}>Error</h1>
        <p>{error}</p>
      </main>
    );
  }

  if (!insight) return null;

  return (
    <main style={{ padding: "2rem", maxWidth: "48rem" }}>
      <Link href="/insights" style={{ color: "#4b5563" }}>
        ← Back to insights
      </Link>

      <div
        style={{
          display: "flex",
          gap: "0.75rem",
          alignItems: "center",
          marginTop: "1rem",
          marginBottom: "0.5rem",
        }}
      >
        <SeverityBadge severity={insight.severity} />
        <code
          style={{
            fontSize: "0.85rem",
            color: "#6b7280",
            backgroundColor: "#f3f4f6",
            padding: "2px 8px",
            borderRadius: 3,
          }}
        >
          {insight.rule_name}
        </code>
      </div>

      <h1 style={{ fontSize: "1.5rem", margin: "0 0 1.5rem 0" }}>
        {insight.title}
      </h1>

      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "auto 1fr",
          gap: "0.5rem 1.5rem",
          fontSize: "0.9rem",
          marginBottom: "2rem",
        }}
      >
        <dt style={{ color: "#6b7280" }}>Insight ID</dt>
        <dd style={{ margin: 0, fontFamily: "monospace", fontSize: "0.8rem" }}>
          {insight.id}
        </dd>

        <dt style={{ color: "#6b7280" }}>Account</dt>
        <dd style={{ margin: 0, fontFamily: "monospace", fontSize: "0.8rem" }}>
          {insight.account_id ?? "—"}
        </dd>

        <dt style={{ color: "#6b7280" }}>Resource</dt>
        <dd style={{ margin: 0, fontFamily: "monospace", fontSize: "0.8rem" }}>
          {insight.resource_id ?? "—"}
        </dd>

        <dt style={{ color: "#6b7280" }}>Computed at</dt>
        <dd style={{ margin: 0 }}>
          {new Date(insight.computed_at).toLocaleString()}
        </dd>
      </dl>

      <h2 style={{ fontSize: "1.1rem", marginBottom: "0.5rem" }}>Payload</h2>
      <pre
        style={{
          backgroundColor: "#f3f4f6",
          padding: "1rem",
          borderRadius: 6,
          overflow: "auto",
          fontSize: "0.85rem",
          lineHeight: 1.5,
        }}
      >
        {JSON.stringify(insight.payload, null, 2)}
      </pre>
    </main>
  );
}
