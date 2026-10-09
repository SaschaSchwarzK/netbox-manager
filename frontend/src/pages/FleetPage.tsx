import { useEffect, useState } from "react";
import { fleetApi, InstanceHealth, GithubTokenStatus, TokenExpiryInfo } from "../api/client";

function daysUntil(iso: string): number | null {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return null;
  return Math.ceil((d.getTime() - Date.now()) / (1000 * 60 * 60 * 24));
}

function ExpiryBadge({ expiry }: { expiry: TokenExpiryInfo }) {
  if (!expiry.known) {
    return <span className="pill" title={expiry.note ?? ""}>unknown</span>;
  }
  if (!expiry.expires) {
    return <span className="pill">no expiry</span>;
  }
  const days = daysUntil(expiry.expires);
  if (days === null) return <span className="pill">{expiry.expires}</span>;
  if (days < 0) return <span className="pill" style={{ color: "var(--danger)", borderColor: "var(--danger)" }}>expired {Math.abs(days)}d ago</span>;
  if (days <= 14) return <span className="pill" style={{ color: "var(--warning)", borderColor: "var(--warning)" }}>expires in {days}d</span>;
  return <span className="pill" style={{ color: "var(--success)" }}>expires in {days}d</span>;
}

export default function FleetPage() {
  const [health, setHealth] = useState<InstanceHealth[]>([]);
  const [githubStatus, setGithubStatus] = useState<GithubTokenStatus[]>([]);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    Promise.all([fleetApi.health(), fleetApi.githubTokenStatus()])
      .then(([h, g]) => { setHealth(h); setGithubStatus(g); })
      .finally(() => setLoading(false));
  };
  useEffect(() => { load(); }, []);

  return (
    <div>
      <h1>Fleet</h1>
      <p className="page-subtitle">NetBox instance reachability, versions, plugins, and token expiry — plus GitHub PAT expiry for your repo targets.</p>

      <div className="toolbar">
        <button className="primary" disabled={loading} onClick={load}>{loading ? "Checking…" : "Refresh"}</button>
      </div>

      <div className="card">
        <h2>NetBox Instances</h2>
        <table>
          <thead>
            <tr><th>Instance</th><th>Status</th><th>Version</th><th>Plugins</th><th>Latency</th><th>Token</th></tr>
          </thead>
          <tbody>
            {health.map((h) => (
              <tr key={h.instance_id}>
                <td>{h.instance_name}</td>
                <td>
                  <span className={`status-dot ${h.reachable ? "status-ok" : "status-error"}`} />
                  {h.reachable ? "reachable" : "unreachable"}
                  {h.error && <div style={{ color: "var(--danger)", fontSize: 11, maxWidth: 260 }}>{h.error}</div>}
                </td>
                <td className="mono">{h.netbox_version ?? "—"}</td>
                <td>
                  {Object.keys(h.plugins ?? {}).length === 0
                    ? "—"
                    : Object.entries(h.plugins).map(([name, version]) => (
                        <span key={name} className="pill" style={{ marginRight: 4 }}>{name} {String(version)}</span>
                      ))}
                </td>
                <td>{h.response_time_ms != null ? `${h.response_time_ms} ms` : "—"}</td>
                <td><ExpiryBadge expiry={h.token_expiry} /></td>
              </tr>
            ))}
            {!loading && health.length === 0 && (
              <tr><td colSpan={6} style={{ color: "var(--muted)" }}>No instances configured yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>GitHub Target Tokens</h2>
        <table>
          <thead>
            <tr><th>Target</th><th>Token</th></tr>
          </thead>
          <tbody>
            {githubStatus.map((g) => (
              <tr key={g.target_id}>
                <td>{g.target_name}</td>
                <td><ExpiryBadge expiry={g.token_expiry} /> {g.token_expiry.note && <span style={{ color: "var(--muted)", fontSize: 12 }}>{g.token_expiry.note}</span>}</td>
              </tr>
            ))}
            {!loading && githubStatus.length === 0 && (
              <tr><td colSpan={2} style={{ color: "var(--muted)" }}>No GitHub targets configured yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
