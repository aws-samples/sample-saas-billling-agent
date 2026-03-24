import React, { useState, useRef, useEffect } from "react";
import type { AgentMessage } from "./App";

// UI strings — extracted for i18n compliance
const UI = {
  welcomeTitle: "Welcome to your Billing Assistant",
  welcomeText: "I can help you understand your API usage, manage invoices, check plan limits, and more. Try one of the actions below or type your own question.",
} as const;

// ── Markdown rendering helpers ─────────────────────────────────────

function parseMarkdownTable(text: string): { headers: string[]; rows: string[][] } | null {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  if (lines.length < 3) return null;
  const isRow = (l: string) => l.startsWith("|") && l.endsWith("|");
  const isSep = (l: string) => /^\|[\s\-:|]+\|$/.test(l);
  let start = -1;
  for (let i = 0; i < lines.length - 1; i++) { if (isRow(lines[i]) && isSep(lines[i + 1])) { start = i; break; } }
  if (start === -1) return null;
  const parse = (l: string) => l.split("|").slice(1, -1).map((c) => c.trim());
  const headers = parse(lines[start]);
  const rows: string[][] = [];
  for (let i = start + 2; i < lines.length; i++) { if (!isRow(lines[i])) break; rows.push(parse(lines[i])); }
  return rows.length > 0 ? { headers, rows } : null;
}

function renderMarkdown(text: string): React.ReactNode {
  // Bold
  let parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    }
    return <span key={i}>{part}</span>;
  });
}

// ── Message Bubble ─────────────────────────────────────────────────

const MessageBubble: React.FC<{ message: AgentMessage }> = ({ message }) => {
  const isUser = message.role === "user";
  const table = !isUser ? parseMarkdownTable(message.content) : null;
  const time = message.timestamp ? new Date(message.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";

  return (
    <div className={`message-row ${message.role}`}>
      {!isUser && <div className="message-avatar agent-avatar">🤖</div>}
      <div className={`message-bubble ${isUser ? "user-bubble" : "agent-bubble"}`}>
        {table ? (
          <>
            {message.content.split("|")[0].trim() && <p style={{ marginBottom: 8 }}>{renderMarkdown(message.content.split("|")[0].trim())}</p>}
            <div className="table-wrapper">
              <table><thead><tr>{table.headers.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
                <tbody>{table.rows.map((row, ri) => <tr key={ri}>{row.map((c, ci) => <td key={ci}>{c}</td>)}</tr>)}</tbody></table>
            </div>
          </>
        ) : (
          <div>{message.content.split("\n").map((line, i) => <React.Fragment key={i}>{i > 0 && <br />}{renderMarkdown(line)}</React.Fragment>)}</div>
        )}
        {message.imageBase64 && <img src={`data:image/png;base64,${message.imageBase64}`} alt="Chart" />}
        {time && <div className="message-time">{time}</div>}
      </div>
      {isUser && <div className="message-avatar user-avatar">👤</div>}
    </div>
  );
};

// ── Chat Widget ────────────────────────────────────────────────────

interface QuickAction { icon: string; label: string; prompt: string; color: string; }

interface ChatWidgetProps {
  messages: AgentMessage[];
  onSendMessage: (text: string) => void;
  loading: boolean;
  quickActions: QuickAction[];
}

const ChatWidget: React.FC<ChatWidgetProps> = ({ messages, onSendMessage, loading, quickActions }) => {
  const [input, setInput] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, loading]);

  const send = (text: string) => { const t = text.trim(); if (!t || loading) return; onSendMessage(t); setInput(""); };

  return (
    <>
      <div className="chat-area">
        {messages.length === 0 ? (
          <div className="chat-empty">
            <div className="empty-hero">
              <div className="empty-icon">💰</div>
              <h2>{UI.welcomeTitle}</h2>
              <p>{UI.welcomeText}</p>
            </div>
            <div className="action-grid">
              {quickActions.map((a) => (
                <button key={a.label} className="action-card" onClick={() => send(a.prompt)} style={{ borderTopColor: a.color }}>
                  <span className="action-card-icon">{a.icon}</span>
                  <span className="action-card-label">{a.label}</span>
                </button>
              ))}
            </div>
          </div>
        ) : (
          <>
            {messages.map((msg, i) => <MessageBubble key={i} message={msg} />)}
            {loading && (
              <div className="message-row agent">
                <div className="message-avatar agent-avatar">🤖</div>
                <div className="typing-indicator">
                  <div className="typing-dots"><span /><span /><span /></div>
                  Thinking...
                </div>
              </div>
            )}
          </>
        )}
        <div ref={endRef} />
      </div>

      <form className="chat-input-area" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <input className="chat-input" value={input} onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about usage, billing, invoices, or your plan..." disabled={loading} />
        <button type="submit" className="btn-send" disabled={loading || !input.trim()}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 2L11 13"/><path d="M22 2L15 22L11 13L2 9L22 2Z"/></svg>
        </button>
      </form>
    </>
  );
};

export default ChatWidget;
