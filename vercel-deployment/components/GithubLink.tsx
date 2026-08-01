"use client";

import { trackGithubClick } from "./AnalyticsBeacon";

export default function GithubLink({
  href,
  className,
  children,
}: {
  href: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={className}
      onClick={() => trackGithubClick()}
    >
      {children}
    </a>
  );
}
