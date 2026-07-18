import Link from "next/link";
import { api, type Inconclusive } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function Home() {
  let inconclusive: Inconclusive[] = [];
  try {
    inconclusive = await api.listInconclusive({ limit: 1 });
  } catch {
    // best-effort: home page shouldn't fail if the API is down
  }

  return (
    <main style={{ padding: "2rem", maxWidth: "48rem" }}>
      <h1 style={{ fontSize: "1.75rem", marginBottom: "0.25rem" }}>Constat</h1>
      <p style={{ color: "#555", marginBottom: "2rem" }}>
        Cloud inventory observability — the écart chiffré.
      </p>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr 1fr",
          gap: "1rem",
        }}
      >
        <Link
          href="/insights"
          style={{
            display: "block",
            padding: "1.5rem",
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            backgroundColor: "#fff",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>Insights</h2>
          <p style={{ margin: 0, color: "#555", fontSize: "0.9rem" }}>
            Proven gaps between what your cloud account should look like and
            what it actually looks like.
          </p>
        </Link>
        <Link
          href="/inconclusives"
          style={{
            display: "block",
            padding: "1.5rem",
            border: "1px solid #fde68a",
            borderRadius: 8,
            backgroundColor: "#fffbeb",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>Inconclusives</h2>
          <p style={{ margin: 0, color: "#555", fontSize: "0.9rem" }}>
            What we can&apos;t conclude yet (missing facts, scope not proven).
          </p>
        </Link>
        <Link
          href="/chargeback"
          style={{
            display: "block",
            padding: "1.5rem",
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            backgroundColor: "#fff",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>Chargeback</h2>
          <p style={{ margin: 0, color: "#555", fontSize: "0.9rem" }}>
            Per-account × service cost, amortized vs brut, sourced from FOCUS.
          </p>
        </Link>
        <Link
          href="/status"
          style={{
            display: "block",
            padding: "1.5rem",
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            backgroundColor: "#fff",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>Status</h2>
          <p style={{ margin: 0, color: "#555", fontSize: "0.9rem" }}>
            One-glance fleet view: counts, freshness, last runs.
          </p>
        </Link>
        <Link
          href="/insights/inbox"
          style={{
            display: "block",
            padding: "1.5rem",
            border: "1px solid #fde68a",
            borderRadius: 8,
            backgroundColor: "#fffbeb",
            textDecoration: "none",
            color: "inherit",
          }}
        >
          <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>Inbox</h2>
          <p style={{ margin: 0, color: "#555", fontSize: "0.9rem" }}>
            Operator triage: ack, start, resolve, dismiss the daily
            insight list.
          </p>
        </Link>
      </section>
    </main>
  );
}
