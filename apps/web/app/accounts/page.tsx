import Link from "next/link";
import { api, ApiError, type Account } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function AccountsPage() {
  let accounts: Account[] = [];
  let error: string | null = null;

  try {
    accounts = await api.listAccounts({ limit: 200 });
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  return (
    <main style={{ padding: "2rem", maxWidth: "56rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.25rem" }}>Accounts</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        {accounts.length === 0
          ? "No accounts observed yet."
          : `${accounts.length} account${accounts.length === 1 ? "" : "s"} known to Constat (from AWS scans and FOCUS ingestion).`}
        <br />
        <Link href="/" style={{ color: "#4b5563", fontSize: "0.85rem" }}>
          ← back to home
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

      {accounts.length > 0 && (
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontSize: "0.9rem",
            backgroundColor: "#fff",
            border: "1px solid #e5e7eb",
            borderRadius: 8,
            overflow: "hidden",
          }}
        >
          <thead style={{ backgroundColor: "#f9fafb" }}>
            <tr>
              <th style={th}>External ID</th>
              <th style={th}>Name</th>
              <th style={th}>First seen</th>
              <th style={th}>Internal ID</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.id} style={{ borderTop: "1px solid #f3f4f6" }}>
                <td style={{ ...td, fontFamily: "monospace" }}>{a.external_id}</td>
                <td style={td}>{a.name ?? "—"}</td>
                <td style={td}>
                  {a.created_at ? new Date(a.created_at).toLocaleString() : "—"}
                </td>
                <td style={{ ...td, fontFamily: "monospace", fontSize: "0.75rem", color: "#6b7280" }}>
                  {a.id.slice(0, 8)}…
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}

const th: React.CSSProperties = {
  textAlign: "left",
  padding: "0.5rem 0.75rem",
  fontSize: "0.75rem",
  color: "#6b7280",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  fontWeight: 600,
};

const td: React.CSSProperties = {
  padding: "0.5rem 0.75rem",
};
