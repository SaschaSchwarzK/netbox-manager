import { Fragment, useEffect, useState } from "react";
import { driftApi, DriftRecord } from "../api/client";
import DiffView, { summarizeDiff } from "../components/DiffView";

const STATUS_COLORS: Record<string, string> = {
  in_sync: "var(--success)",
  drift: "var(--warning)",
  missing: "var(--danger)",
  error: "var(--danger)",
};

export default function DriftPage() {
  const [records, setRecords] = useState<DriftRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = () => { setLoading(true); driftApi.list().then(setRecords).finally(() => setLoading(false)); };
  useEffect(() => { load(); }, []);

  const handleCheckNow = async () => {
    setChecking(true);
    try {
      const results = await driftApi.checkNow();
      setRecords(results);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div>
      <h1>Drift</h1>
      <p className="page-subtitle">
        Compares what's actually on each NetBox instance against the GitHub source of truth, for every
        device type that's been pushed there before. Checked automatically on a schedule, or on demand below.
      </p>

      <div className="toolbar">
        <button className="primary" disabled={checking} onClick={handleCheckNow}>
          {checking ? "Checking…" : "Check all now"}
        </button>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr><th>Instance</th><th>Device Type</th><th>Repo</th><th>Status</th><th>Changes</th><th>Last checked</th><th></th></tr>
          </thead>
          <tbody>
            {records.map((r) => (
              <Fragment key={r.id}>
                <tr>
                  <td>{r.instance_name}</td>
                  <td className="mono">{r.file_path}</td>
                  <td>{r.repo_target_name}</td>
                  <td>
                    <span className="status-dot" style={{ background: STATUS_COLORS[r.status] ?? "var(--muted)" }} />
                    {r.status}
                  </td>
                  <td style={{ color: "var(--muted)", fontSize: 12.5 }}>{r.diff ? summarizeDiff(r.diff) : "—"}</td>
                  <td>{new Date(r.checked_at).toLocaleString()}</td>
                  <td>
                    {r.diff && (
                      <button onClick={() => setExpanded(expanded === r.id ? null : r.id)} style={{ padding: "2px 8px" }}>
                        {expanded === r.id ? "Hide" : "View diff"}
                      </button>
                    )}
                  </td>
                </tr>
                {expanded === r.id && r.diff && (
                  <tr>
                    <td colSpan={7} style={{ background: "var(--panel-raised)" }}>
                      <DiffView diff={r.diff} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {!loading && records.length === 0 && (
              <tr><td colSpan={7} style={{ color: "var(--muted)" }}>
                No drift data yet — nothing's been pushed to a NetBox instance yet, or a check hasn't run. Click "Check all now".
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
