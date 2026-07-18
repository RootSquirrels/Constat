import type { Severity } from "@/lib/api";

const COLORS: Record<Severity, { bg: string; fg: string }> = {
  critical: { bg: "#fee2e2", fg: "#991b1b" },
  warning: { bg: "#fef3c7", fg: "#92400e" },
  info: { bg: "#dbeafe", fg: "#1e40af" },
};

export default function SeverityBadge({ severity }: { severity: Severity }) {
  const c = COLORS[severity];
  return (
    <span
      style={{
        backgroundColor: c.bg,
        color: c.fg,
        padding: "2px 8px",
        borderRadius: 4,
        fontSize: "0.75rem",
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        display: "inline-block",
      }}
    >
      {severity}
    </span>
  );
}
