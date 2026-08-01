"use client";

import { useEffect, useState } from "react";

type State = "checking" | "ok" | "error";

/** Live proof the three-tier path is real: a same-origin fetch through
 * CloudFront's VPC origin to the internal ALB -> Fargate app tier. */
export default function HealthPill() {
  const [state, setState] = useState<State>("checking");
  const [detail, setDetail] = useState("Checking app tier...");

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((body) => {
        if (cancelled) return;
        setState("ok");
        setDetail(`App tier reachable (status: ${body.status ?? "ok"})`);
      })
      .catch(() => {
        if (cancelled) return;
        setState("error");
        setDetail("App tier unreachable");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const color =
    state === "ok"
      ? "text-emerald-300 border-emerald-400/30 bg-emerald-400/10"
      : state === "error"
      ? "text-red-300 border-red-400/30 bg-red-400/10"
      : "text-twin-muted border-white/15 bg-white/5";

  return (
    <div className="flex items-center gap-3 text-sm">
      <span className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 font-semibold backdrop-blur-xl ${color}`}>
        <span className="h-2 w-2 rounded-full bg-current" />
        {detail}
      </span>
      <span className="font-mono text-xs text-twin-muted">GET /api/health</span>
    </div>
  );
}
