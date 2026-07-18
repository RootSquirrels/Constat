export default function ChargebackPage() {
  return (
    <main style={{ padding: "2rem", maxWidth: "48rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.5rem" }}>Chargeback</h1>
      <p style={{ color: "#555" }}>
        Per-account × service cost chargeback, sourced from FOCUS exports.
        The <code>cost.*</code> facts flow from the FOCUS ingestion CLI into
        the <code>focus_charges</code> table; the <code>chargeback</code>{" "}
        insight rule aggregates them and emits per-service drift.
      </p>

      <section
        style={{
          marginTop: "2rem",
          padding: "1.5rem",
          border: "1px solid #e5e7eb",
          borderRadius: 8,
          backgroundColor: "#fff",
        }}
      >
        <h2 style={{ marginTop: 0, fontSize: "1.1rem" }}>To populate data</h2>
        <ol style={{ marginBottom: 0, lineHeight: 1.8 }}>
          <li>
            Export a FOCUS 1.0 CSV from your AWS account (CUR with FOCUS
            columns).
          </li>
          <li>
            Run the ingestion CLI:
            <pre
              style={{
                backgroundColor: "#f3f4f6",
                padding: "0.75rem",
                borderRadius: 4,
                margin: "0.5rem 0",
                fontSize: "0.85rem",
              }}
            >
              python -m constat_api.cli.focus --account 111111111111 --csv focus.csv
            </pre>
          </li>
          <li>
            Trigger the <code>chargeback</code> insight rule (next commit) — it
            reads <code>focus_charges</code> and emits per-service Insights
            with the amortized-vs-billed drift.
          </li>
        </ol>
      </section>
    </main>
  );
}
