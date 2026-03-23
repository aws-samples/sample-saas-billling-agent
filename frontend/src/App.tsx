import React, { useEffect, useState, useCallback, useRef } from "react";
import { Amplify } from "aws-amplify";
import { signIn, signOut, getCurrentUser, fetchAuthSession, type SignInInput } from "@aws-amplify/auth";
import { amplifyConfig, AGENT_RUNTIME_URL, AGENT_RUNTIME_ARN } from "./config";
import ChatWidget from "./ChatWidget";

Amplify.configure(amplifyConfig);

export interface AuthState {
  isAuthenticated: boolean;
  tenantId: string | null;
  username: string | null;
  jwtToken: string | null;
}

export interface AgentMessage {
  role: "user" | "agent";
  content: string;
  imageBase64?: string;
  timestamp?: number;
}

interface ConversationSession {
  id: string;
  label: string;
  messages: AgentMessage[];
  createdAt: number;
}

function generateSessionId(): string {
  return `billing-session-${Date.now()}-${Math.random().toString(36).slice(2, 15)}`;
}

function loadSessions(): ConversationSession[] {
  try {
    const raw = localStorage.getItem("billing_sessions");
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function saveSessions(sessions: ConversationSession[]) {
  try { localStorage.setItem("billing_sessions", JSON.stringify(sessions.slice(0, 20))); } catch { /* quota */ }
}

export async function getAuthToken(): Promise<{ token: string; tenantId: string } | null> {
  try {
    const session = await fetchAuthSession();
    const accessToken = session.tokens?.accessToken;
    const idToken = session.tokens?.idToken;
    if (!accessToken) return null;
    const tokenStr = accessToken.toString();
    const tenantId = (idToken?.payload?.["custom:tenant_id"] as string) ?? (idToken?.payload?.["tenant_id"] as string) ?? "unknown";
    return { token: tokenStr, tenantId };
  } catch { return null; }
}

export async function sendMessageToAgent(message: string, jwtToken: string, tenantId: string, sessionId?: string): Promise<AgentMessage> {
  const encodedArn = encodeURIComponent(AGENT_RUNTIME_ARN);
  const url = `${AGENT_RUNTIME_URL}/runtimes/${encodedArn}/invocations?qualifier=DEFAULT`;
  const headers: Record<string, string> = { "Content-Type": "application/json", Authorization: `Bearer ${jwtToken}`, "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Tenant-Id": tenantId };
  if (sessionId) headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = sessionId;

  const response = await fetch(url, { method: "POST", headers, body: JSON.stringify({ prompt: message }) });
  if (!response.ok) { const t = await response.text().catch(() => ""); throw new Error(`${response.status} ${t}`); }
  const data = await response.json();
  return { role: "agent", content: data.response ?? data.result ?? JSON.stringify(data), imageBase64: data.image_base64, timestamp: Date.now() };
}

const QUICK_ACTIONS = [
  { icon: "📊", label: "Usage Summary", prompt: "Show me my API usage summary for this month in a table", color: "#4f46e5" },
  { icon: "📈", label: "Usage Trend", prompt: "Show my usage trend for the last 3 months with month-over-month growth percentages", color: "#0891b2" },
  { icon: "📉", label: "Usage Chart", prompt: "Use code_interpreter to execute Python code that creates a bar chart of my API usage for the last 3 months using matplotlib. You must run the code.", color: "#0d9488" },
  { icon: "🧾", label: "Invoice History", prompt: "Show all my invoices with status, amount, and due date in a table", color: "#7c3aed" },
  { icon: "💳", label: "Current Balance", prompt: "What is my current billing balance?", color: "#059669" },
  { icon: "📋", label: "My Plan", prompt: "What is my current plan? Show limits for API calls and data transfer", color: "#d97706" },
  { icon: "⚠️", label: "Quota Check", prompt: "Check my quota — show usage vs limits as percentages and warn if I am approaching any limit", color: "#dc2626" },
  { icon: "🔄", label: "Plan Comparison", prompt: "Show all available plans in a comparison table with pricing, API limits, and data transfer limits", color: "#2563eb" },
  { icon: "💡", label: "Upgrade Advice", prompt: "Analyze my current usage against my plan limits and recommend whether I should upgrade. Show the numbers.", color: "#9333ea" },
  { icon: "🧠", label: "Recall Memory", prompt: "What do you remember about me from our previous conversations? What are my preferences?", color: "#6366f1" },
  { icon: "🥧", label: "Endpoint Breakdown", prompt: "Use code_interpreter to execute Python code that creates a pie chart showing my API usage breakdown by endpoint for this month. You must run the code.", color: "#f59e0b" },
  { icon: "📊", label: "Cost Projection", prompt: "Use code_interpreter to execute Python code that projects my costs for next month based on the last 3 months trend using linear regression. Show the chart and numbers.", color: "#ef4444" },
];

const App: React.FC = () => {
  const [auth, setAuth] = useState<AuthState>({ isAuthenticated: false, tenantId: null, username: null, jwtToken: null });
  const [sessions, setSessions] = useState<ConversationSession[]>(loadSessions);
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    const saved = loadSessions();
    return saved.length > 0 ? saved[0].id : generateSessionId();
  });
  const [messages, setMessages] = useState<AgentMessage[]>(() => {
    const saved = loadSessions();
    return saved.length > 0 ? saved[0].messages : [];
  });
  const [loading, setLoading] = useState(false);
  const [loginForm, setLoginForm] = useState({ username: "", password: "" });
  const [loginError, setLoginError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [actionsExpanded, setActionsExpanded] = useState(false);
  const sessionIdRef = useRef(loadSessions()[0]?.id ?? generateSessionId());

  // Persist messages to sessions whenever they change
  useEffect(() => {
    setSessions((prev) => {
      const idx = prev.findIndex((s) => s.id === activeSessionId);
      let updated: ConversationSession[];
      if (idx >= 0) {
        updated = [...prev];
        updated[idx] = { ...updated[idx], messages };
      } else if (messages.length > 0) {
        const firstMsg = messages[0]?.content?.slice(0, 40) || "New chat";
        updated = [{ id: activeSessionId, label: firstMsg, messages, createdAt: Date.now() }, ...prev];
      } else {
        return prev;
      }
      saveSessions(updated);
      return updated;
    });
  }, [messages, activeSessionId]);

  useEffect(() => {
    (async () => {
      try {
        const user = await getCurrentUser();
        const tokenInfo = await getAuthToken();
        if (tokenInfo) setAuth({ isAuthenticated: true, tenantId: tokenInfo.tenantId, username: user.username, jwtToken: tokenInfo.token });
      } catch { /* not signed in */ }
    })();
  }, []);

  const handleSignIn = useCallback(async () => {
    setLoginError(null);
    try {
      await signIn({ username: loginForm.username, password: loginForm.password } as SignInInput);
      const user = await getCurrentUser();
      const tokenInfo = await getAuthToken();
      if (tokenInfo) setAuth({ isAuthenticated: true, tenantId: tokenInfo.tenantId, username: user.username, jwtToken: tokenInfo.token });
    } catch (err: unknown) { setLoginError(err instanceof Error ? err.message : "Sign-in failed"); }
  }, [loginForm]);

  const handleSignOut = useCallback(async () => {
    await signOut();
    setAuth({ isAuthenticated: false, tenantId: null, username: null, jwtToken: null });
    setMessages([]);
  }, []);

  const handleNewChat = useCallback(() => {
    const newId = generateSessionId();
    sessionIdRef.current = newId;
    setActiveSessionId(newId);
    setMessages([]);
  }, []);

  const handleSwitchSession = useCallback((session: ConversationSession) => {
    sessionIdRef.current = session.id;
    setActiveSessionId(session.id);
    setMessages(session.messages);
  }, []);

  const handleDeleteSession = useCallback((sessionId: string) => {
    setSessions((prev) => {
      const updated = prev.filter((s) => s.id !== sessionId);
      saveSessions(updated);
      return updated;
    });
    if (sessionId === activeSessionId) {
      handleNewChat();
    }
  }, [activeSessionId, handleNewChat]);

  const handleSendMessage = useCallback(async (text: string) => {
    if (!auth.jwtToken) return;
    setMessages((prev) => [...prev, { role: "user", content: text, timestamp: Date.now() }]);
    setLoading(true);
    try {
      const tokenInfo = await getAuthToken();
      const agentMsg = await sendMessageToAgent(text, tokenInfo?.token ?? auth.jwtToken, tokenInfo?.tenantId ?? auth.tenantId ?? "unknown", sessionIdRef.current);
      setMessages((prev) => [...prev, agentMsg]);
    } catch (err) {
      setMessages((prev) => [...prev, { role: "agent", content: `Sorry, something went wrong: ${err instanceof Error ? err.message : "Unknown error"}`, timestamp: Date.now() }]);
    } finally { setLoading(false); }
  }, [auth.jwtToken, auth.tenantId]);

  if (!auth.isAuthenticated) {
    return (
      <div className="login-page">
        <div className="login-card">
          <div className="login-logo">
            <div className="login-logo-icon">💰</div>
            <h1>SaaS Billing Agent</h1>
          </div>
          <p className="login-subtitle">AI-powered billing intelligence. Manage usage, invoices, and plans through natural conversation.</p>
          <div className="login-form">
            <div className="form-group">
              <label htmlFor="username">Username</label>
              <input id="username" className="form-input" value={loginForm.username}
                onChange={(e) => setLoginForm((f) => ({ ...f, username: e.target.value }))} placeholder="Enter your username" />
            </div>
            <div className="form-group">
              <label htmlFor="password">Password</label>
              <input id="password" type="password" className="form-input" value={loginForm.password}
                onChange={(e) => setLoginForm((f) => ({ ...f, password: e.target.value }))} placeholder="Enter your password"
                onKeyDown={(e) => e.key === "Enter" && handleSignIn()} />
            </div>
            <button className="btn-primary" onClick={handleSignIn}>Sign In</button>
            {loginError && <div className="login-error">{loginError}</div>}
          </div>
          <div className="login-footer">Powered by Amazon Bedrock AgentCore</div>
        </div>
      </div>
    );
  }

  return (
    <div className="app-layout">
      <aside className={`sidebar ${sidebarOpen ? "open" : "closed"}`}>
        <div className="sidebar-header">
          <div className="sidebar-logo">
            <span className="sidebar-logo-icon">💰</span>
            <span className="sidebar-logo-text">Billing Agent</span>
          </div>
          <button className="sidebar-toggle" onClick={() => setSidebarOpen(!sidebarOpen)} aria-label="Toggle sidebar">
            {sidebarOpen ? "◀" : "▶"}
          </button>
        </div>

        <button className="btn-new-chat" onClick={handleNewChat}>
          <span>＋</span> New Conversation
        </button>

        <div className="sidebar-scroll">
          {/* Conversation History */}
          <div className="sidebar-group">
            <div className="sidebar-group-header">
              <span>Recent Chats</span>
              {sessions.length > 0 && <span className="sidebar-badge">{sessions.length}</span>}
            </div>
            {sessions.length === 0 ? (
              <div className="sidebar-empty">No conversations yet</div>
            ) : (
              sessions.map((s) => (
                <div key={s.id} className={`sidebar-session ${s.id === activeSessionId ? "active" : ""}`}>
                  <button className="sidebar-session-btn" onClick={() => handleSwitchSession(s)} title={s.label}>
                    <span className="sidebar-session-icon">{s.id === activeSessionId ? "▶" : "💬"}</span>
                    <div className="sidebar-session-text">
                      <span className="sidebar-session-label">{s.label.replace(/^\[Context:.*?\]\s*/, "").slice(0, 28)}{s.label.length > 28 ? "…" : ""}</span>
                      <span className="sidebar-session-meta">{s.messages.length} msgs · {new Date(s.createdAt).toLocaleDateString()}</span>
                    </div>
                  </button>
                  <button className="sidebar-session-delete" onClick={(e) => { e.stopPropagation(); handleDeleteSession(s.id); }} title="Delete" aria-label="Delete conversation">×</button>
                </div>
              ))
            )}
          </div>

          {/* Quick Actions */}
          <div className="sidebar-group">
            <button className="sidebar-group-header sidebar-group-toggle" onClick={() => setActionsExpanded(!actionsExpanded)}>
              <span>Quick Actions</span>
              <span className="sidebar-chevron">{actionsExpanded ? "▾" : "▸"}</span>
            </button>
            {actionsExpanded && (
              <div className="sidebar-actions-grid">
                {QUICK_ACTIONS.map((a) => (
                  <button key={a.label} className="sidebar-action-chip" onClick={() => handleSendMessage(a.prompt)} disabled={loading} title={a.prompt}>
                    <span>{a.icon}</span>
                    <span>{a.label}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="sidebar-footer">
          <div className="sidebar-user">
            <div className="sidebar-user-avatar">👤</div>
            <div className="sidebar-user-info">
              <div className="sidebar-user-name">{auth.username}</div>
              <div className="sidebar-user-tenant">{auth.tenantId}</div>
            </div>
          </div>
          <button className="btn-signout-sm" onClick={handleSignOut}>Sign Out</button>
        </div>
      </aside>

      <main className="main-content">
        <header className="app-header">
          <button className="hamburger" onClick={() => setSidebarOpen(!sidebarOpen)} aria-label="Menu">☰</button>
          <div className="header-center">
            <div className="header-title">SaaS Billing Assistant</div>
            <div className="header-subtitle">{messages.length} messages</div>
          </div>
          <button className="btn-new-chat-sm" onClick={handleNewChat} title="New conversation">＋</button>
        </header>
        <div className="session-info-bar">
          <span className="session-info-item">🏢 <span className="session-info-label">Tenant:</span> {auth.tenantId}</span>
          <span className="session-info-item">🔗 <span className="session-info-label">Session:</span> {activeSessionId.slice(-12)}</span>
          <span className="session-info-item">💬 <span className="session-info-label">Messages:</span> {messages.length}</span>
        </div>
        <ChatWidget messages={messages} onSendMessage={handleSendMessage} loading={loading} quickActions={QUICK_ACTIONS} />
      </main>
    </div>
  );
};

export default App;
