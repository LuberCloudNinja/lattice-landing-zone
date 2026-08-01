"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import CosmicBackground from "@/components/CosmicBackground";
import SiteSideMenu from "@/components/SiteSideMenu";

export default function SiteFrame({
  children,
  title,
  subtitle,
  eyebrow = "Cloud Architecture Portfolio",
}: {
  children: ReactNode;
  title?: ReactNode;
  subtitle?: ReactNode;
  eyebrow?: string;
}) {
  const pathname = usePathname();
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);

  useEffect(() => {
    setIsMobileMenuOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!isMobileMenuOpen) return;
    const original = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = original;
    };
  }, [isMobileMenuOpen]);

  return (
    <main id="top" className="relative min-h-screen overflow-hidden bg-[#020617] text-[var(--twin-text)]">
      <div className="fixed inset-0 z-0">
        <CosmicBackground />
      </div>
      <div className="pointer-events-none fixed inset-0 z-10 bg-[radial-gradient(circle_at_15%_0%,rgba(91,157,255,0.16),transparent_42%),radial-gradient(circle_at_85%_5%,rgba(124,108,240,0.13),transparent_40%),radial-gradient(circle_at_50%_100%,rgba(242,169,59,0.10),transparent_36%)]" />
      <div className="scanlines fixed inset-0 z-10" aria-hidden="true" />

      <aside className="hidden lg:block fixed left-5 top-5 z-30 w-64 rounded-2xl border border-white/20 bg-white/[0.06] p-4 shadow-[0_12px_40px_rgba(2,6,23,0.45)] backdrop-blur-3xl">
        <p className="mb-3 font-mono text-[11px] uppercase tracking-[0.2em] text-[#b6ccff]">Site Menu</p>
        <SiteSideMenu />
      </aside>

      <div className="fixed left-4 top-4 z-40 lg:hidden">
        <button
          type="button"
          onClick={() => setIsMobileMenuOpen(true)}
          className="inline-flex items-center gap-2 rounded-xl border border-white/25 bg-[#06142acc] px-3 py-2 font-mono text-[11px] uppercase tracking-[0.14em] text-[#e4edff] shadow-[0_10px_28px_rgba(2,6,23,0.45)] backdrop-blur-2xl"
          aria-label="Open site menu"
        >
          Menu
        </button>
      </div>

      {isMobileMenuOpen && (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            onClick={() => setIsMobileMenuOpen(false)}
            className="absolute inset-0 bg-[#010713cc]"
            aria-label="Close menu overlay"
          />
          <aside className="absolute left-0 top-0 h-full w-[84vw] max-w-[20rem] border-r border-white/20 bg-[#071326f2] p-4 shadow-[0_24px_80px_rgba(0,0,0,0.55)] backdrop-blur-3xl">
            <div className="mb-4 flex items-center justify-between">
              <p className="font-mono text-[11px] uppercase tracking-[0.2em] text-[#b6ccff]">Site Menu</p>
              <button
                type="button"
                onClick={() => setIsMobileMenuOpen(false)}
                className="rounded-lg border border-white/20 bg-white/[0.06] p-2 text-[#dbe6ff]"
                aria-label="Close menu"
              >
                Close
              </button>
            </div>
            <SiteSideMenu onNavigate={() => setIsMobileMenuOpen(false)} />
          </aside>
        </div>
      )}

      <div className="relative z-20 lg:pl-[18.5rem]">
        <section className="px-4 pb-8 pt-20 md:px-8 md:pt-10 lg:pt-8">
          {title && (
            <div className="rounded-3xl border border-white/20 bg-white/[0.05] p-6 backdrop-blur-2xl shadow-[0_24px_90px_rgba(0,0,0,0.45)] md:p-8">
              <p className="font-mono text-[11px] uppercase tracking-[0.24em] text-[#b8ceff]">{eyebrow}</p>
              <h1 className="mt-2 text-3xl font-semibold md:text-5xl">
                <span className="inline-block bg-gradient-to-r from-[#d5e7ff] via-[#7db2ff] to-[#9f8dff] bg-clip-text text-transparent drop-shadow-[0_10px_30px_rgba(125,178,255,0.18)]">
                  {title}
                </span>
              </h1>
              {subtitle && (
                <p className="mt-3 max-w-3xl text-sm leading-relaxed text-[#c9d6f2] md:text-base">{subtitle}</p>
              )}
            </div>
          )}

          <div className={`rounded-3xl border border-white/15 bg-white/[0.04] p-5 backdrop-blur-xl md:p-7 ${title ? "mt-7" : ""}`}>
            {children}
          </div>
        </section>

        <footer className="px-4 pb-8 text-center font-mono text-xs text-[#9fb2d8] md:px-8">
          Built by Luber J Guilarte Hay
        </footer>
      </div>
    </main>
  );
}
