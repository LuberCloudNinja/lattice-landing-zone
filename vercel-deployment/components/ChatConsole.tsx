"use client";

import { useEffect, useRef, useState } from "react";

type Role = "user" | "assistant";
type Message = { role: Role; text: string };

const CONVERSATION_KEY = "lattice-lab-conversation-id";

function conversationId(): string {
  if (typeof window === "undefined") return "ssr";
  let id = window.sessionStorage.getItem(CONVERSATION_KEY);
  if (!id) {
    id = crypto.randomUUID();
    window.sessionStorage.setItem(CONVERSATION_KEY, id);
  }
  return id;
}

const SUGGESTIONS = [
  "How does traffic get inspected before reaching a workload?",
  "What does the VPC Lattice service mesh actually do?",
  "How is the CDK pipeline deployed and approved?",
  "What can the AI agents in this project read and write?",
];

export default function ChatConsole() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: "assistant",
      text: "Ask me anything about this project, the network design, the CDK stacks, or the AI layer. I answer from the project's own documentation.",
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, loading]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    setError(false);
    setMessages((prev) => [...prev, { role: "user", text: trimmed }]);
    setInput("");
    setLoading(true);

    try {
      const res = await fetch("/api/assistant/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed, history: messages, conversationId: conversationId() }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json();
      setMessages((prev) => [...prev, { role: "assistant", text: body.answer || "No answer returned." }]);
    } catch {
      setError(true);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          text: "The assistant hit a snag reaching its model provider. Give it a moment and try again.",
        },
      ]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      data-chat-frame="1"
      className="flex flex-col h-[520px] rounded-xl border border-white/20 bg-white/[0.06] backdrop-blur-2xl overflow-hidden"
    >
      <div ref={listRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.map((m, i) => (
          <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            <div
              className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
                m.role === "user"
                  ? "bg-[#5b9dff33] border border-[#5b9dff55] text-[#e9f1ff]"
                  : "bg-white/[0.06] border border-white/15 text-[#d9e5ff]"
              }`}
            >
              {m.text}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="rounded-2xl border border-white/15 bg-white/[0.06] px-4 py-3 flex gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-[#9fc2ff] animate-bounce-dot" />
              <span className="h-1.5 w-1.5 rounded-full bg-[#9fc2ff] animate-bounce-dot delay-100" />
              <span className="h-1.5 w-1.5 rounded-full bg-[#9fc2ff] animate-bounce-dot delay-200" />
            </div>
          </div>
        )}
      </div>

      {messages.length < 2 && (
        <div className="px-4 pb-2 flex flex-wrap gap-2">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => send(s)}
              className="rounded-full border border-white/15 bg-white/[0.04] px-3 py-1.5 text-xs text-twin-muted hover:border-[#5b9dff60] hover:text-[#d9e5ff] transition"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
        className="border-t border-white/15 p-3 flex gap-2"
      >
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about the network, the mesh, the pipeline, the AI layer..."
          className="input-focus-glow flex-1 rounded-xl border border-white/15 bg-white/[0.04] px-4 py-2.5 text-sm text-twin-text placeholder:text-twin-muted outline-none"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="rounded-xl border border-[#5b9dff60] bg-[#5b9dff22] px-4 py-2.5 text-sm font-semibold text-[#d9e5ff] disabled:opacity-40 hover:bg-[#5b9dff33] transition"
        >
          Ask
        </button>
      </form>
      {error && (
        <p className="px-4 pb-3 text-xs text-[#f2a93b]">
          Tip: this deployment needs OPENAI_API_KEY set in its Vercel project settings.
        </p>
      )}
    </div>
  );
}
