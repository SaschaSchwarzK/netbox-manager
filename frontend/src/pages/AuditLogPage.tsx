import { useEffect, useState } from "react";
import { auditApi, AuditLogEntry } from "../api/client";

export default function AuditLogPage() {
  const [entries, setEntries] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    auditApi.list().then(setEntries).finally(() => setLoading(false));
  }, []);

  return (
    <div>
      <h1>Audit Log</h1>
      <p className="page-subtitle">
        Every GitHub save (create/update/import) and NetBox push, with who did it and when. Anonymous
        entries mean auth was disabled at the time of the action, not that logging failed.
      </p>

      <div className="card">
        <table>
          <thead>
            <tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Device Type</th><th>Status</th><th>Detail</th></tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.id}>
                <td>{new Date(e.created_at).toLocaleString()}</td>
                <td>
                  {e.actor_name ?? "—"}
                  {e.actor_email && <div style={{ color: "var(--muted)", fontSize: 11 }}>{e.actor_email}</div>}
                </td>
                <td><span className="pill">{e.action_type === "netbox" ? "push to NetBox" : "GitHub save"}</span></td>
                <td>{e.target_name}</td>
                <td className="mono">{e.file_path}</td>
                <td>
                  <span className={`status-dot ${e.status === "success" ? "status-ok" : "status-error"}`} />
                  {e.status}
                </td>
                <td style={{ maxWidth: 320 }}>{e.detail ?? "—"}</td>
              </tr>
            ))}
            {!loading && entries.length === 0 && (
              <tr><td colSpan={7} style={{ color: "var(--muted)" }}>No actions logged yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
