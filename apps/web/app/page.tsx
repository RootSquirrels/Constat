export default function Home() {
  return (
    <main style={{ padding: "2rem", maxWidth: "48rem" }}>
      <h1 style={{ fontSize: "1.75rem", marginBottom: "0.5rem" }}>Constat</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        Cloud inventory observability — the écart chiffré.
      </p>
      <section>
        <h2 style={{ fontSize: "1.1rem" }}>V1 foundations</h2>
        <p style={{ color: "#555" }}>
          This is the first commit. The Insights and Chargeback views will be wired in
          follow-up commits. The API skeleton lives at <code>apps/api</code>.
        </p>
      </section>
    </main>
  );
}
