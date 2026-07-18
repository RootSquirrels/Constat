import Link from "next/link";
import type { Insight } from "@/lib/api";
import SeverityBadge from "./SeverityBadge";

export default function InsightCard({ insight }: { insight: Insight }) {
  return (
    <article
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        padding: "1rem",
        marginBottom: "0.75rem",
        backgroundColor: "#fff",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: "0.5rem",
          alignItems: "center",
          marginBottom: "0.5rem",
        }}
      >
        <SeverityBadge severity={insight.severity} />
        <code
          style={{
            fontSize: "0.75rem",
            color: "#6b7280",
            backgroundColor: "#f3f4f6",
            padding: "1px 6px",
            borderRadius: 3,
          }}
        >
          {insight.rule_name}
        </code>
        {insight.account_id && (
          <span style={{ fontSize: "0.75rem", color: "#6b7280" }}>
            acct: {insight.account_id.slice(0, 8)}…
          </span>
        )}
      </div>
      <h3 style={{ margin: "0 0 0.5rem 0", fontSize: "1rem" }}>
        <Link
          href={`/insights/${insight.id}`}
          style={{ color: "#111827", textDecoration: "none" }}
        >
          {insight.title}
        </Link>
      </h3>
      <p style={{ margin: 0, fontSize: "0.8rem", color: "#6b7280" }}>
        {new Date(insight.computed_at).toLocaleString()}
      </p>
    </article>
  );
}
