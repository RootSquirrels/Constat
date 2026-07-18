import Link from "next/link";
import { api, ApiError, type Inconclusive } from "@/lib/api";
import InconclusiveCard from "@/components/InconclusiveCard";

export const dynamic = "force-dynamic";

function groupByReason(items: Inconclusive[]): Record<string, Inconclusive[]> {
  const groups: Record<string, Inconclusive[]> = {};
  for (const i of items) {
    const key = i.reason ?? "unknown";
    if (!groups[key]) groups[key] = [];
    groups[key].push(i);
  }
  return groups;
}

export default async function InconclusivesPage() {
  let items: Inconclusive[] = [];
  let error: string | null = null;

  try {
    items = await api.listInconclusive({ limit: 200 });
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  const groups = groupByReason(items);
  const total = items.length;
  const reasonKeys = Object.keys(groups).sort();

  return (
    <main style={{ padding: "2rem", maxWidth: "56rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.25rem" }}>Inconclusives</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        {total === 0
          ? "No inconclusive records."
          : `${total} record${total === 1 ? "" : "s"} where the rule could not conclude.`}
        {" "}
        <Link href="/insights" style={{ color: "#4b5563" }}>
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

      {reasonKeys.map((reason) => (
        <section key={reason} style={{ marginBottom: "2rem" }}>
          <h2
            style={{
              fontSize: "1.1rem",
              marginBottom: "0.75rem",
              color: "#374151",
              textTransform: "capitalize",
            }}
          >
            {reason.replace(/_/g, " ")}{" "}
            <span style={{ color: "#9ca3af", fontWeight: 400 }}>
              ({groups[reason].length})
            </span>
          </h2>
          {groups[reason].map((i) => (
            <InconclusiveCard key={i.id} item={i} />
          ))}
        </section>
      ))}
    </main>
  );
}
