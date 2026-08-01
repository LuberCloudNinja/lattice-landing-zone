import type { Config } from "tailwindcss";

// This site commits deliberately to one visual world (a dark, cosmic
// control-room aesthetic, matching the layout of luberguilarte's other
// portfolio project), not a light/dark toggle.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        twin: {
          bg: "#020617",
          text: "#eef1f6",
          muted: "#8a97b3",
          accent: "#5b9dff",
          accent2: "#7c6cf0",
          warm: "#f2a93b",
        },
      },
      fontFamily: {
        sans: ["var(--font-plex-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-plex-mono)", "ui-monospace", "SF Mono", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
