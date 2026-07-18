"use client";

// App Router error boundary body, shared by the data pages' error.tsx
// (which must be client components and own the `reset` callback).
export default function PageError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <main style={{ padding: "2rem", maxWidth: "48rem" }}>
      <h1 style={{ fontSize: "1.5rem", marginBottom: "0.5rem" }}>
        Something went wrong
      </h1>
      <p style={{ color: "#991b1b" }}>{error.message}</p>
      <button
        onClick={reset}
        style={{
          padding: "0.5rem 1rem",
          border: "1px solid #e5e7eb",
          borderRadius: 6,
          backgroundColor: "#fff",
          cursor: "pointer",
        }}
      >
        Try again
      </button>
    </main>
  );
}
