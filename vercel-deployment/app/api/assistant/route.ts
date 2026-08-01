import { NextRequest, NextResponse } from "next/server";
import { blogSections } from "@/content/blogSections";

// Standalone Vercel copy of the blog assistant. The AWS version
// (stacks/assets/blog_assistant/handler.py) retrieves from a Bedrock
// Knowledge Base backed by S3 Vectors. This copy has no AWS behind it at
// all, so it grounds the same way with a much simpler approach: the whole
// blog corpus (both the airport-analogy and technical paragraphs, already
// exported as content/blogSections.ts for the page itself) goes straight
// into the system prompt. The corpus is small enough (well under 40k
// tokens) to fit a single request comfortably, so there is no retrieval
// step and nothing to index.

const MODEL = process.env.OPENAI_MODEL || "gpt-4o-mini";

function buildContext(): string {
  return blogSections
    .map((s) => {
      const parts = [`# ${s.title}`];
      if (s.airport.length) parts.push(s.airport.join("\n\n"));
      if (s.technicalTitle) parts.push(`## ${s.technicalTitle}`);
      if (s.technical.length) parts.push(s.technical.join("\n\n"));
      return parts.join("\n\n");
    })
    .join("\n\n---\n\n");
}

const SYSTEM_PROMPT = `You are the assistant embedded in Luber J Guilarte Hay's portfolio blog about the lattice-landing-zone project, a hybrid AWS landing zone built in CDK. You are a genuine expert in cloud architecture, AWS networking, security, containers, and the AI and machine learning services used in this project. Visitors will ask two kinds of questions: some are about this specific project (its stacks, its design decisions, why something was built a certain way), and some are broader questions about the underlying technologies it uses, such as VPC Lattice, Transit Gateway, Cloud WAN, PrivateLink, Gateway Load Balancer, Bedrock, SageMaker, Kafka, ECS, Lambda, or CDK in general. Reference material from this project's own blog content is provided below. Use it as the authoritative source for anything about this specific project. For general questions about the technologies themselves, beyond what the material covers, answer from your own knowledge of AWS and cloud architecture the same way you would in any technical conversation. Only say you do not know when a question is genuinely unrelated to cloud, software, or this project, and in that case suggest the visitor read the full blog post or the source code on GitHub. Explain things in plain, direct sentences aimed at a technical reader. Do not use em dashes or en dashes as punctuation, use periods and commas instead. Keep answers focused and conversational, a few sentences to a couple of short paragraphs, going deeper when the question is genuinely technical.

Reference material from the project's own blog:
${buildContext()}`;

type HistoryTurn = { role: "user" | "assistant"; text: string };

export async function POST(req: NextRequest) {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    return NextResponse.json(
      { error: "OPENAI_API_KEY is not configured on this deployment." },
      { status: 500 },
    );
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json" }, { status: 400 });
  }

  const { question, history } = body as { question?: unknown; history?: unknown };
  const trimmedQuestion = typeof question === "string" ? question.trim() : "";
  if (!trimmedQuestion) {
    return NextResponse.json({ error: "missing question" }, { status: 400 });
  }

  const turns: HistoryTurn[] = Array.isArray(history)
    ? history
        .filter(
          (m): m is HistoryTurn =>
            !!m &&
            typeof m === "object" &&
            (m.role === "user" || m.role === "assistant") &&
            typeof m.text === "string",
        )
        .slice(-16)
    : [];

  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    ...turns.map((t) => ({ role: t.role, content: t.text })),
    { role: "user", content: trimmedQuestion },
  ];

  let upstream: Response;
  try {
    upstream = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model: MODEL,
        messages,
        max_tokens: 900,
        temperature: 0.4,
      }),
    });
  } catch (err) {
    console.error("OpenAI request failed", err);
    return NextResponse.json({ error: "upstream request failed" }, { status: 502 });
  }

  if (!upstream.ok) {
    const errText = await upstream.text().catch(() => "");
    console.error("OpenAI error", upstream.status, errText);
    return NextResponse.json({ error: "upstream error" }, { status: 502 });
  }

  const data = await upstream.json();
  const answer: string = data?.choices?.[0]?.message?.content?.trim() || "No answer returned.";
  return NextResponse.json({ answer });
}
