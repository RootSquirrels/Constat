import type { Inconclusive } from "@/lib/api";

export default function InconclusiveCard({ item }: { item: Inconclusive }) {
  return (
    <article
      style={{
        border: "1px solid #fde68a",
        borderRadius: 8,
        padding: "1rem",
        marginBottom: "0.75rem",
        backgroundColor: "#fffbeb",
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
        <span
          style={{
            backgroundColor: "#fde68a",
            color: "#92400e",
            padding: "2px 8px",
            borderRadius: 4,
            fontSize: "0.75rem",
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.05em",
            display: "inline-block",
          }}
        >
          INCONCLUSIVE
        </span>
        <code
          style={{
            fontSize: "0.75rem",
            color: "#6b7280",
            backgroundColor: "rgba(0,0,0,0.05)",
            padding: "1px 6px",
            borderRadius: 3,
          }}
        >
          {item.rule_name}
        </code>
        {item.account_id && (
          <span style={{ fontSize: "0.75rem", color: "#6b7280" }}>
            acct: {item.account_id.slice(0, 8)}…
          </span>
        )}
      </div>
      <h3 style={{ margin: "0 0 0.5rem 0", fontSize: "1rem", color: "#92400e" }}>
        {item.reason ?? "Missing data — could not conclude"}
      </h3>
      <p style={{ margin: "0 0 0.5rem 0", fontSize: "0.85rem", color: "#374151" }}>
        <strong>Missing facts:</strong>{" "}
        <code style={{ background: "rgba(0,0,0,0.05)", padding: "1px 4px", borderRadius: 3 }}>
          {item.missing_facts.join(", ") || "<none>"}
        </code>
      </p>
      <p style={{ margin: 0, fontSize: "0.8rem", color: "#6b7280" }}>
        {new Date(item.computed_at).toLocaleString()}
      </p>
    </article>
  );
}
