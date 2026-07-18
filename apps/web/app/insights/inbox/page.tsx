import Link from "next/link";
import { api, ApiError, type AckStatus, type Insight, type Severity } from "@/lib/api";
import AckButtons from "@/components/AckButtons";
import SeverityBadge from "@/components/SeverityBadge";

export const dynamic = "force-dynamic";

const ACK_STATES: { value: AckStatus; label: string; color: string }[] = [
  { value: "acknowledged", label: "Acknowledged", color: "#1e40af" },
  { value: "in_progress", label: "In progress", color: "#92400e" },
  { value: "resolved", label: "Resolved", color: "#166534" },
  { value: "dismissed", label: "Dismissed", color: "#6b7280" },
];

export default async function InsightsInboxPage() {
  // The operator's flow is "what's left to triage?" + "what's in
  // flight?" + "what's been closed?". Resolved/dismissed live in the
  // /insights page; the inbox focuses on the live pipeline.
  let open: Insight[] = [];
  let acknowledged: Insight[] = [];
  let inProgress: Insight[] = [];
  let error: string | null = null;

  try {
    const [openRes, ackRes, ipRes] = await Promise.all([
      api.listInsights({ ack_status: "open", limit: 200 }),
      api.listInsights({ ack_status: "acknowledged", limit: 50 }),
      api.listInsights({ ack_status: "in_progress", limit: 50 }),
    ]);
    open = openRes;
    acknowledged = ackRes;
    inProgress = ipRes;
  } catch (e) {
    error = e instanceof ApiError ? `API ${e.status}: ${e.body}` : String(e);
  }

  if (error) {
    return (
      <main style={{ padding: "2rem", maxWidth: "56rem" }}>
        <h1 style={{ fontSize: "1.5rem", marginBottom: "1rem" }}>Inbox</h1>
        <div
          style={{
            padding: "1rem",
            border: "1px solid #fecaca",
            backgroundColor: "#fef2f2",
            color: "#991b1b",
            borderRadius: 8,
          }}
        >
          <strong>API error.</strong> {error}
        </div>
      </main>
    );
  }

  const totalOpen = open.length;
  const totalAcknowledged = acknowledged.length;
  const totalInProgress = inProgress.length;
  const totalToTriage = totalOpen + totalAcknowledged;

  return (
    <main style={{ padding: "2rem", maxWidth: "64rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.25rem" }}>Inbox</h1>
      <p style={{ color: "#555", marginBottom: "1.5rem" }}>
        Operator triage for the daily insight list. {totalToTriage} insight
        {totalToTriage === 1 ? "" : "s"} need attention
        {totalInProgress > 0 ? `, ${totalInProgress} in progress` : ""}.
        <br />
        <Link href="/insights" style={{ color: "#4b5563", fontSize: "0.85rem" }}>
          ← back to all insights
        </Link>
      </p>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: "1rem",
          marginBottom: "2rem",
        }}
      >
        <SummaryCard
          label="Open (not triaged)"
          count={totalOpen}
          color={totalOpen > 0 ? "#991b1b" : "#166534"}
        />
        <SummaryCard
          label="Acknowledged"
          count={totalAcknowledged}
          color="#1e40af"
        />
        <SummaryCard
          label="In progress"
          count={totalInProgress}
          color="#92400e"
        />
      </section>

      {totalOpen > 0 && (
        <InboxSection
          title="Open — not triaged"
          subtitle="The daily intake. Acknowledge to start triage, Dismiss if false positive, Resolve if already fixed."
          items={open}
          criticalOnly
        />
      )}
      {totalAcknowledged > 0 && (
        <InboxSection
          title="Acknowledged"
          subtitle="Someone has seen these. The next step is to mark them In progress (work started) or Dismiss."
          items={acknowledged}
        />
      )}
      {totalInProgress > 0 && (
        <InboxSection
          title="In progress"
          subtitle="Active work. Resolve when fixed, or move back to Acknowledged if blocked."
          items={inProgress}
        />
      )}

      {totalToTriage === 0 && totalInProgress === 0 && (
        <p style={{ color: "#166534", fontStyle: "italic" }}>
          Inbox empty. Nothing to triage. Resolved and dismissed insights
          live in <Link href="/insights">all insights</Link>.
        </p>
      )}
    </main>
  );
}


function SummaryCard({
  label,
  count,
  color,
}: {
  label: string;
  count: number;
  color: string;
}) {
  return (
    <div
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        padding: "1rem",
        backgroundColor: "#fff",
      }}
    >
      <div
        style={{
          fontSize: "0.75rem",
          color: "#6b7280",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: "1.5rem",
          fontWeight: 600,
          color,
          marginTop: "0.25rem",
        }}
      >
        {count}
      </div>
    </div>
  );
}


function InboxSection({
  title,
  subtitle,
  items,
  criticalOnly = false,
}: {
  title: string;
  subtitle: string;
  items: Insight[];
  criticalOnly?: boolean;
}) {
  const filtered = criticalOnly
    ? items.filter((i) => i.severity === "critical")
    : items;

  if (filtered.length === 0) return null;

  // Group by severity within the section so criticals show first.
  const bySev: Record<Severity, Insight[]> = { critical: [], warning: [], info: [] };
  for (const i of filtered) bySev[i.severity].push(i);

  return (
    <section style={{ marginBottom: "2rem" }}>
      <h2 style={{ fontSize: "1.1rem", marginBottom: "0.25rem" }}>
        {title}{" "}
        <span style={{ color: "#9ca3af", fontWeight: 400 }}>({filtered.length})</span>
      </h2>
      <p
        style={{
          color: "#6b7280",
          fontSize: "0.85rem",
          marginBottom: "0.75rem",
        }}
      >
        {subtitle}
      </p>
      {(["critical", "warning", "info"] as Severity[]).map((sev) => (
        <div key={sev} style={{ marginBottom: "1rem" }}>
          {bySev[sev].length > 0 && (
            <h3
              style={{
                fontSize: "0.85rem",
                textTransform: "uppercase",
                color: "#6b7280",
                marginBottom: "0.5rem",
                letterSpacing: "0.05em",
              }}
            >
              {sev} ({bySev[sev].length})
            </h3>
          )}
          {bySev[sev].map((i) => (
            <InboxRow key={i.id} insight={i} />
          ))}
        </div>
      ))}
    </section>
  );
}


function InboxRow({ insight }: { insight: Insight }) {
  return (
    <article
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: 8,
        padding: "0.75rem 1rem",
        marginBottom: "0.5rem",
        backgroundColor: "#fff",
        display: "flex",
        gap: "1rem",
        alignItems: "flex-start",
      }}
    >
      <div style={{ flex: 1 }}>
        <div
          style={{
            display: "flex",
            gap: "0.5rem",
            alignItems: "center",
            marginBottom: "0.25rem",
          }}
        >
          <SeverityBadge severity={insight.severity} />
          <code
            style={{
              fontSize: "0.7rem",
              color: "#6b7280",
              backgroundColor: "#f3f4f6",
              padding: "1px 6px",
              borderRadius: 3,
            }}
          >
            {insight.rule_name}
          </code>
          {insight.ack_by && (
            <span style={{ fontSize: "0.7rem", color: "#6b7280" }}>
              by {insight.ack_by}
            </span>
          )}
        </div>
        <h3 style={{ margin: "0 0 0.25rem 0", fontSize: "0.95rem" }}>
          <Link
            href={`/insights/${insight.id}`}
            style={{ color: "#111827", textDecoration: "none" }}
          >
            {insight.title}
          </Link>
        </h3>
        <p style={{ margin: 0, fontSize: "0.75rem", color: "#6b7280" }}>
          {new Date(insight.computed_at).toLocaleString()}
          {insight.ack_at && (
            <>
              {" · acked "}
              {new Date(insight.ack_at).toLocaleString()}
            </>
          )}
        </p>
      </div>
      <AckButtons insight={insight} />
    </article>
  );
}
