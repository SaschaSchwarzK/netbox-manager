import { DiffResult } from "../api/client";

const COMPONENT_LABELS: Record<string, string> = {
  "interfaces": "Interfaces",
  "console-ports": "Console Ports",
  "console-server-ports": "Console Server Ports",
  "power-ports": "Power Ports",
  "power-outlets": "Power Outlets",
  "rear-ports": "Rear Ports",
  "front-ports": "Front Ports",
  "device-bays": "Device Bays",
  "module-bays": "Module Bays",
};

export default function DiffView({ diff }: { diff: DiffResult }) {
  if (diff.status === "missing") {
    return <p style={{ fontSize: 13, color: "var(--warning)" }}>Not present on this instance yet — would be created.</p>;
  }
  if (diff.status === "in_sync") {
    return <p style={{ fontSize: 13, color: "var(--success)" }}>In sync with GitHub.</p>;
  }

  return (
    <div style={{ fontSize: 13 }}>
      {diff.base_field_changes.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <p style={{ color: "var(--muted)", marginBottom: 4 }}>Base fields:</p>
          <table>
            <thead><tr><th>Field</th><th>GitHub</th><th>NetBox</th></tr></thead>
            <tbody>
              {diff.base_field_changes.map((c) => (
                <tr key={c.field}>
                  <td className="mono">{c.field}</td>
                  <td className="mono">{JSON.stringify(c.source)}</td>
                  <td className="mono">{JSON.stringify(c.netbox)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {Object.entries(diff.component_changes).map(([key, change]) => (
        <div key={key} style={{ marginBottom: 6 }}>
          <span style={{ color: "var(--muted)" }}>{COMPONENT_LABELS[key] ?? key}: </span>
          {change.added.length > 0 && <span style={{ color: "var(--success)" }}>+{change.added.length} new ({change.added.join(", ")}) </span>}
          {change.removed.length > 0 && <span style={{ color: "var(--danger)" }}>-{change.removed.length} removed ({change.removed.join(", ")}) </span>}
          {change.changed.length > 0 && <span style={{ color: "var(--warning)" }}>~{change.changed.length} changed ({change.changed.join(", ")})</span>}
        </div>
      ))}
    </div>
  );
}
