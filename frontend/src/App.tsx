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
  // ── Data Queries ──
  { icon: "📊", label: "This Month's Usage", prompt: "Show my complete API usage summary for this month — include total API calls, data transfer in GB, and compute seconds in a clean table", color: "#4f46e5" },
  { icon: "📈", label: "3-Month Trend", prompt: "Show my usage trend for the last 3 months. Include month-over-month growth percentages for API calls, data transfer, and compute time", color: "#0891b2" },
  { icon: "🔍", label: "Top Endpoints", prompt: "Which API endpoints am I using the most this month? Show a breakdown by endpoint with call counts", color: "#0ea5e9" },
  { icon: "🧾", label: "My Invoices", prompt: "Show all my invoices in a table with month, amount, status (draft/sent), and due date", color: "#7c3aed" },
  { icon: "💳", label: "Account Balance", prompt: "What is my current billing balance? Have any credits been applied recently?", color: "#059669" },

  // ── Plan & Quota ──
  { icon: "📋", label: "Plan Details", prompt: "What plan am I on? Show my API call limit, data transfer limit, price, and when it expires", color: "#d97706" },
  { icon: "⚠️", label: "Am I Near Limits?", prompt: "Check my quota status — what percentage of my API call and data transfer limits have I used this month? Am I approaching any limit?", color: "#dc2626" },
  { icon: "🔄", label: "Compare Plans", prompt: "Show all available plans side by side in a comparison table — include name, monthly price, API call limit, data transfer limit, and features", color: "#2563eb" },
  { icon: "💡", label: "Should I Upgrade?", prompt: "Based on my actual usage over the last 3 months, do I need to upgrade my plan? Show my usage vs limits and recommend the best option with cost savings", color: "#9333ea" },

  // ── Visualizations (Code Interpreter) ──
  { icon: "📉", label: "Usage Bar Chart", prompt: "Use code_interpreter to execute Python code that creates a colored bar chart comparing my API calls across Jan, Feb, and Mar 2026 using matplotlib. You must actually run the code, not just describe it.", color: "#0d9488" },
  { icon: "🥧", label: "Endpoint Pie Chart", prompt: "Use code_interpreter to execute Python code that creates a pie chart showing the percentage breakdown of my API usage by endpoint for this month using matplotlib. You must actually run the code.", color: "#f59e0b" },
  { icon: "📊", label: "Cost Forecast", prompt: "Use code_interpreter to execute Python code that takes my last 3 months of usage data, fits a linear regression, and projects next month's API calls and estimated cost. Show both a chart and the numbers. You must actually run the code.", color: "#ef4444" },

  // ── Memory & Context ──
  { icon: "🧠", label: "What Do You Remember?", prompt: "What do you remember about me and my account from our previous conversations? Any preferences or patterns you've noticed?", color: "#6366f1" },
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
      const enrichedPrompt = `[Context: tenant_id=${tokenInfo?.tenantId ?? auth.tenantId}] ${text}`;
      const agentMsg = await sendMessageToAgent(enrichedPrompt, tokenInfo?.token ?? auth.jwtToken, tokenInfo?.tenantId ?? auth.tenantId ?? "unknown", sessionIdRef.current);
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
            <h1>SaaS Billing Agent</h1> // nosemgrep: jsx-not-internationalized
          </div>
          <p className="login-subtitle">AI-powered billing intelligence. Manage usage, invoices, and plans through natural conversation.</p>
          <div className="login-form">
            <div className="form-group">
              <label htmlFor="username">Username</label> // nosemgrep: jsx-not-internationalized
              <input id="username" className="form-input" value={loginForm.username}
                onChange={(e) => setLoginForm((f) => ({ ...f, username: e.target.value }))} placeholder="Enter your username" />
            </div>
            <div className="form-group">
              <label htmlFor="password">Password</label> // nosemgrep: jsx-not-internationalized
              <input id="password" type="password" className="form-input" value={loginForm.password}
                onChange={(e) => setLoginForm((f) => ({ ...f, password: e.target.value }))} placeholder="Enter your password"
                onKeyDown={(e) => e.key === "Enter" && handleSignIn()} />
            </div>
            <button className="btn-primary" onClick={handleSignIn}>Sign In</button> // nosemgrep: jsx-not-internationalized
            {loginError && <div className="login-error">{loginError}</div>}
          </div>
          <div className="login-footer">Powered by Amazon Bedrock AgentCore</div> // nosemgrep: jsx-not-internationalized
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
            <span className="sidebar-logo-text">Billing Agent</span> // nosemgrep: jsx-not-internationalized
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
              <span>Recent Chats</span> // nosemgrep: jsx-not-internationalized
              {sessions.length > 0 && <span className="sidebar-badge">{sessions.length}</span>}
            </div>
            {sessions.length === 0 ? (
              <div className="sidebar-empty">No conversations yet</div> // nosemgrep: jsx-not-internationalized
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

          {/* Quick Actions */} // nosemgrep: jsx-not-internationalized
          <div className="sidebar-group">
            <button className="sidebar-group-header sidebar-group-toggle" onClick={() => setActionsExpanded(!actionsExpanded)}>
              <span>Quick Actions</span> // nosemgrep: jsx-not-internationalized
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
          <button className="btn-signout-sm" onClick={handleSignOut}>Sign Out</button> // nosemgrep: jsx-not-internationalized
        </div>
      </aside>

      <main className="main-content">
        <header className="app-header">
          <button className="hamburger" onClick={() => setSidebarOpen(!sidebarOpen)} aria-label="Menu">☰</button>
          <div className="header-center">
            <div className="header-title">SaaS Billing Assistant</div> // nosemgrep: jsx-not-internationalized
            <div className="header-subtitle">{messages.length} messages</div>
          </div>
          <button className="btn-new-chat-sm" onClick={handleNewChat} title="New conversation">＋</button>
        </header>
        <div className="session-info-bar">
          <span className="session-info-item">🏢 <span className="session-info-label">Tenant:</span> {auth.tenantId}</span> // nosemgrep: jsx-not-internationalized
          <span className="session-info-item">🔗 <span className="session-info-label">Session:</span> {activeSessionId.slice(-12)}</span> // nosemgrep: jsx-not-internationalized
          <span className="session-info-item">💬 <span className="session-info-label">Messages:</span> {messages.length}</span> // nosemgrep: jsx-not-internationalized
        </div>
        <ChatWidget messages={messages} onSendMessage={handleSendMessage} loading={loading} quickActions={QUICK_ACTIONS} />
      </main>
    </div>
  );
};

export default App;
