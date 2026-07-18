import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Constat",
  description: "Cloud inventory observability — the écart chiffré",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          fontFamily: "system-ui, -apple-system, sans-serif",
          backgroundColor: "#f9fafb",
          color: "#111827",
        }}
      >
        <nav
          style={{
            padding: "0.75rem 2rem",
            borderBottom: "1px solid #e5e7eb",
            backgroundColor: "#fff",
            display: "flex",
            gap: "1.5rem",
            alignItems: "center",
          }}
        >
          <Link
            href="/"
            style={{ fontWeight: 600, color: "#111827", textDecoration: "none" }}
          >
            Constat
          </Link>
          <Link
            href="/insights"
            style={{ color: "#4b5563", textDecoration: "none", fontSize: "0.9rem" }}
          >
            Insights
          </Link>
          <Link
            href="/inconclusives"
            style={{ color: "#4b5563", textDecoration: "none", fontSize: "0.9rem" }}
          >
            Inconclusives
          </Link>
          <Link
            href="/chargeback"
            style={{ color: "#4b5563", textDecoration: "none", fontSize: "0.9rem" }}
          >
            Chargeback
          </Link>
        </nav>
        {children}
      </body>
    </html>
  );
}
