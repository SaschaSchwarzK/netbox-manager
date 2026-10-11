import { useEffect, useState } from "react";
import { auditApi, AuditLogEntry } from "../api/client";

export default function AuditLogPage() {
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    auditApi.list()
      .then(setEntries)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <h1>Audit Log</h1>
      <p className="page-subtitle">
        Meaningful user activities, including login and logout, template changes, NetBox pushes,
        exports, and data migrations.
      </p>

      {error && <div className="error-banner">Could not load the audit log: {error}</div>}

      <div className="card">
        <table>
          <thead>
            <tr><th>When</th><th>Who</th><th>Feature</th><th>Activity</th><th>Status</th></tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td>{new Date(e.created_at).toLocaleString()}</td>
                <td>
                  {e.actor_name ?? "—"}
                  {e.actor_email && <div style={{ color: "var(--muted)", fontSize: 11 }}>{e.actor_email}</div>}
                </td>
                <td><span className="pill">{e.feature}</span></td>
                <td style={{ maxWidth: 560 }}>{e.activity}</td>
                <td>
                  <span className={`status-dot ${e.status === "success" ? "status-ok" : "status-error"}`} />
                  {e.status}
                </td>
              </tr>
            ))}
            {!loading && !error && entries.length === 0 && (
              <tr><td colSpan={5} style={{ color: "var(--muted)" }}>No activities logged yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
