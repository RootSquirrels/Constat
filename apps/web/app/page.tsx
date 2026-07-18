import Link from "next/link";

export default function Home() {
  return (
    <main style={{ padding: "2rem", maxWidth: "48rem" }}>
      <h1 style={{ fontSize: "1.75rem", marginBottom: "0.25rem" }}>Constat</h1>
      <p style={{ color: "#555", marginBottom: "2rem" }}>
        Cloud inventory observability — the écart chiffré.
      </p>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
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
      </section>
    </main>
  );
}
