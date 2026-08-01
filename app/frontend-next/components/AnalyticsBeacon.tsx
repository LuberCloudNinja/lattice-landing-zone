"use client";

import { useEffect, useRef } from "react";

/**
 * Lightweight, privacy-conscious read-analytics beacon for one blog post.
 *
 * Backend: BlogAnalyticsStack (API Gateway -> Lambda -> DynamoDB), routed
 * through the SAME CloudFront distribution as this site at /analytics/* --
 * same-origin, same pattern as HealthPill's /api/*, no CORS needed. Country
 * comes from CloudFront's own CloudFront-Viewer-Country header (no
 * third-party geo-IP lookup); the Lambda derives the caller's IP from the
 * request itself rather than the client claiming one. Every send() call is
 * wrapped so analytics can never break the reading experience -- a failed
 * or missing endpoint just means no data point, not a broken page.
 *
 * Session id lives in sessionStorage (cleared when the tab closes) --
 * enough to tell "did this visit read the whole post," not a persistent
 * cross-visit tracking cookie.
 */

const SESSION_KEY = "lattice-lab-session-id";

function sessionId(): string {
  if (typeof window === "undefined") return "ssr";
  let id = window.sessionStorage.getItem(SESSION_KEY);
  if (!id) {
    id = crypto.randomUUID();
    window.sessionStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

function send(path: string, body: Record<string, unknown>, useBeacon = false) {
  const url = `/analytics${path}`;
  const payload = JSON.stringify({ ...body, sessionId: sessionId(), path: window.location.pathname, ts: Date.now() });
  try {
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([payload], { type: "application/json" }));
    } else {
      fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: payload, keepalive: true }).catch(() => {});
    }
  } catch {
    // analytics must never break the reading experience
  }
}

export function trackGithubClick() {
  send("/events", { type: "github_click" });
}

export default function AnalyticsBeacon({ articleSelector = "article" }: { articleSelector?: string }) {
  const maxScroll = useRef(0);
  const startedAt = useRef(0);
  const completed = useRef(false);

  useEffect(() => {
    startedAt.current = Date.now();
    send("/events", { type: "page_view" });

    const onScroll = () => {
      const el = document.querySelector(articleSelector);
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const total = rect.height - window.innerHeight;
      const scrolled = total > 0 ? Math.min(100, Math.max(0, ((-rect.top) / total) * 100) | 0) : 100;
      if (scrolled > maxScroll.current) maxScroll.current = scrolled;
      if (scrolled >= 95 && !completed.current) {
        completed.current = true;
        send("/events", { type: "read_complete" });
      }
    };

    const onLeave = () => {
      send(
        "/events",
        {
          type: "session_end",
          timeOnPageMs: Date.now() - startedAt.current,
          maxScrollPercent: maxScroll.current,
          completedRead: completed.current,
        },
        true
      );
    };

    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pagehide", onLeave);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") onLeave();
    });

    return () => {
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("pagehide", onLeave);
    };
  }, [articleSelector]);

  return null;
}
