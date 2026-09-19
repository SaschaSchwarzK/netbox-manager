export default function LoginScreen() {
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
        <a href="/api/auth/login">
          <button className="primary" style={{ width: "100%" }}>Sign in with SSO</button>
        </a>
      </div>
    </div>
  );
}
