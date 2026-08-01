import { NextResponse } from "next/server";

// Genuinely meaningful here, unlike a static export: this is a real Next.js
// server route running on Vercel, so a 200 here proves the app tier -- the
// same process /api/assistant runs on -- is actually alive, not a facade.
export async function GET() {
  return NextResponse.json({ status: "ok" });
}
