"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

function normalizePath(path: string) {
  if (!path || path === "/") return "/";
  return path.endsWith("/") ? path.slice(0, -1) : path;
}

export const siteMenuItems = [
  { label: "Home", href: "/" },
  { label: "The Full Story", href: "/blog/hybrid-cloud-airport-story/" },
  { label: "Ask The Architecture", href: "/#assistant-console" },
  { label: "Architecture Diagrams", href: "https://lubercloudninja.github.io/lattice-landing-zone/", external: true },
  { label: "Project Source", href: "https://github.com/LuberCloudNinja/lattice-landing-zone", external: true },
];

export default function SiteSideMenu({
  compact = false,
  onNavigate,
}: {
  compact?: boolean;
  onNavigate?: () => void;
}) {
  const pathname = usePathname();

  return (
    <nav
      className={
        compact
          ? "flex gap-2 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          : "flex flex-col gap-2"
      }
      aria-label="Primary navigation"
    >
      {siteMenuItems.map((item) => {
        const active = !item.external && normalizePath(pathname) === normalizePath(item.href);
        const className = [
          compact
            ? "shrink-0 whitespace-nowrap rounded-xl border px-3 py-2 text-[11px] font-medium tracking-[0.12em] uppercase transition backdrop-blur-xl"
            : "rounded-xl border px-3 py-2 text-xs font-medium tracking-[0.14em] uppercase transition backdrop-blur-xl",
          active
            ? "border-[#9dc2ff90] bg-[#9dc2ff24] bg-gradient-to-r from-[#ffd7a3] via-[#ffbf71] to-[#ffd7a3] bg-clip-text text-transparent shadow-[0_0_0_1px_rgba(157,194,255,0.25),0_6px_18px_rgba(14,116,255,0.22)]"
            : "border-white/20 bg-white/[0.05] bg-gradient-to-r from-[#ffd7a3] via-[#ffbf71] to-[#ffd7a3] bg-clip-text text-transparent hover:border-white/40 hover:bg-white/[0.12]",
        ].join(" ");

        if (item.external) {
          return (
            <a
              key={item.href}
              href={item.href}
              target="_blank"
              rel="noopener noreferrer"
              className={className}
              onClick={onNavigate}
            >
              {item.label}
            </a>
          );
        }

        return (
          <Link key={item.href} href={item.href} onClick={onNavigate} className={className}>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
