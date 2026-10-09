import { FormEvent, useState } from "react";
import { authApi, AuthMeResponse } from "../api/client";

export default function LoginScreen({ auth }: { auth: AuthMeResponse }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleLocalLogin = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await authApi.localLogin(username, password);
      window.location.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: "var(--bg)", color: "var(--text)",
    }}>
      <div className="card" style={{ width: 360, textAlign: "center" }}>
        <div style={{ fontWeight: 600, fontSize: 18, marginBottom: 4 }}>
          netbox<span style={{ color: "var(--accent)" }}>manager</span>
        </div>
        <p style={{ color: "var(--muted)", fontSize: 13, marginBottom: 20 }}>Sign in to continue.</p>

        {auth.oidc_enabled && (
          <a href="/api/auth/login">
            <button className="primary" style={{ width: "100%" }}>Sign in with SSO</button>
          </a>
        )}

        {auth.local_login_enabled && (
          <form onSubmit={handleLocalLogin} style={{ marginTop: auth.oidc_enabled ? 20 : 0, textAlign: "left" }}>
            <p style={{ color: "var(--muted)", fontSize: 12 }}>
              Break-glass local administrator login
            </p>
            <div className="form-row">
              <label>Username</label>
              <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
            </div>
            <div className="form-row">
              <label>Password</label>
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
              />
            </div>
            {error && <p style={{ color: "var(--danger)", fontSize: 12 }}>{error}</p>}
            <button className="primary" style={{ width: "100%" }} disabled={submitting || !username || !password}>
              {submitting ? "Signing in…" : "Sign in as local admin"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
