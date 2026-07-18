"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, type AckStatus, type Insight } from "@/lib/api";

const ACTIONS: { value: AckStatus; label: string; color: string }[] = [
  { value: "acknowledged", label: "Ack", color: "#1e40af" },
  { value: "in_progress", label: "Start", color: "#92400e" },
  { value: "resolved", label: "Resolve", color: "#166534" },
  { value: "dismissed", label: "Dismiss", color: "#6b7280" },
];

export default function AckButtons({ insight }: { insight: Insight }) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const handleClick = (ack_status: AckStatus) => {
    setError(null);
    startTransition(async () => {
      try {
        await api.patchInsight(insight.id, ack_status, "operator@constat");
        // The PATCH succeeded — re-render the page server-side. The
        // /insights/inbox page reads from the API on every request
        // (force-dynamic), so a router.refresh() pulls the updated state.
        router.refresh();
      } catch (e) {
        const msg =
          e instanceof ApiError
            ? `API ${e.status}: ${e.body}`
            : String(e);
        setError(msg);
      }
    });
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "0.25rem",
        alignItems: "flex-end",
      }}
    >
      <div style={{ display: "flex", gap: "0.25rem", flexWrap: "wrap" }}>
        {ACTIONS.map((a) => (
          <button
            key={a.value}
            type="button"
            disabled={isPending}
            onClick={() => handleClick(a.value)}
            style={{
              padding: "0.25rem 0.5rem",
              fontSize: "0.75rem",
              backgroundColor: "#fff",
              color: a.color,
              border: `1px solid ${a.color}`,
              borderRadius: 4,
              cursor: isPending ? "wait" : "pointer",
              opacity: isPending ? 0.5 : 1,
            }}
          >
            {a.label}
          </button>
        ))}
      </div>
      {error && (
        <div style={{ fontSize: "0.7rem", color: "#991b1b", maxWidth: 200 }}>
          {error}
        </div>
      )}
    </div>
  );
}
