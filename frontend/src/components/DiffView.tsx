import { DiffResult, ChangedItem, CustomFieldsDiffResult } from "../api/client";

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

// The per-key differences inside a "custom_fields" base-field change (itself a
// dict-vs-dict comparison), broken out so the diff view can show which
// specific custom field values differ instead of two whole-object JSON blobs.
function customFieldRowChanges(source: any, netbox: any): { key: string; source: any; netbox: any }[] {
  const s = source ?? {};
  const n = netbox ?? {};
  const keys = Array.from(new Set([...Object.keys(s), ...Object.keys(n)]));
  return keys
    .filter((k) => JSON.stringify(s[k] ?? null) !== JSON.stringify(n[k] ?? null))
    .sort()
    .map((k) => ({ key: k, source: s[k], netbox: n[k] }));
}

// A one-line summary of a diff, for a table row that hasn't been expanded yet.
export function summarizeDiff(diff: DiffResult): string {
  if (diff.status === "missing") return "Not on instance yet";
  if (diff.status === "in_sync") return "In sync";

  const parts: string[] = [];
  const cfChange = diff.base_field_changes.find((c) => c.field === "custom_fields");
  if (cfChange) {
    const n = customFieldRowChanges(cfChange.source, cfChange.netbox).length;
    if (n > 0) parts.push(`${n} custom field${n === 1 ? "" : "s"}`);
  }
  const otherBaseFields = diff.base_field_changes.filter((c) => c.field !== "custom_fields").length;
  if (otherBaseFields > 0) parts.push(`${otherBaseFields} base field${otherBaseFields === 1 ? "" : "s"}`);

  const componentChangeCount = Object.values(diff.component_changes)
    .reduce((sum, ch) => sum + ch.added.length + ch.removed.length + ch.changed.length, 0);
  if (componentChangeCount > 0) parts.push(`${componentChangeCount} component change${componentChangeCount === 1 ? "" : "s"}`);

  return parts.length > 0 ? parts.join(", ") : "Differs";
}

export function ChangedItemsList({ items }: { items: ChangedItem[] }) {
  if (items.length === 0) return null;
  return (
    <div style={{ marginLeft: 12 }}>
      {items.map((item) => (
        <div key={item.name} style={{ marginBottom: 2 }}>
          <span className="mono" style={{ color: "var(--warning)" }}>{item.name}</span>
          {": "}
          {item.field_changes.map((fc, i) => (
            <span key={fc.field} className="mono" style={{ marginRight: 8 }}>
              {i > 0 && ", "}
              {fc.field} <span style={{ color: "var(--muted)" }}>{JSON.stringify(fc.source)} → {JSON.stringify(fc.existing)}</span>
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}

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
              {diff.base_field_changes.map((c) => c.field === "custom_fields" ? (
                <tr key={c.field}>
                  <td className="mono">custom_fields</td>
                  <td colSpan={2}>
                    {customFieldRowChanges(c.source, c.netbox).map((cf) => (
                      <div key={cf.key} className="mono" style={{ marginBottom: 2 }}>
                        {cf.key}: {JSON.stringify(cf.source ?? null)} → {JSON.stringify(cf.netbox ?? null)}
                      </div>
                    ))}
                  </td>
                </tr>
              ) : (
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
        <div key={key} style={{ marginBottom: 8 }}>
          <div style={{ color: "var(--muted)" }}>{COMPONENT_LABELS[key] ?? key}:</div>
          {change.added.length > 0 && (
            <div style={{ marginLeft: 12, color: "var(--success)" }}>+{change.added.length} new: {change.added.join(", ")}</div>
          )}
          {change.removed.length > 0 && (
            <div style={{ marginLeft: 12, color: "var(--danger)" }}>-{change.removed.length} removed: {change.removed.join(", ")}</div>
          )}
          <ChangedItemsList items={change.changed} />
        </div>
      ))}
    </div>
  );
}

// A one-line summary of a custom-fields template diff, mirroring summarizeDiff above.
export function summarizeCustomFieldsDiff(diff: CustomFieldsDiffResult): string {
  if (diff.status === "in_sync") return "In sync";
  if (diff.status === "error") return "Error checking";

  const parts: string[] = [];
  (["custom_fields", "custom_field_choice_sets"] as const).forEach((key) => {
    const d = diff[key];
    if (!d) return;
    const n = d.missing_on_instance.length + d.extra_on_instance.length + d.changed.length;
    if (n > 0) parts.push(`${n} ${key === "custom_fields" ? "field" : "choice set"}${n === 1 ? "" : "s"}`);
  });
  return parts.length > 0 ? parts.join(", ") : "Differs";
}

// Shared between the Custom Fields page's on-demand check and the Drift page's
// persisted "custom_fields"-kind records, so both render identically.
export function CustomFieldsDiffView({ diff }: { diff: CustomFieldsDiffResult }) {
  if (diff.status === "error") return <p style={{ color: "var(--danger)", fontSize: 13 }}>Could not check this instance.</p>;
  if (diff.status === "in_sync") return <p style={{ color: "var(--success)", fontSize: 13 }}>In sync with template.</p>;

  return (
    <div style={{ fontSize: 13 }}>
      {(["custom_fields", "custom_field_choice_sets"] as const).map((key) => {
        const d = diff[key];
        if (!d || (!d.missing_on_instance.length && !d.extra_on_instance.length && !d.changed.length)) return null;
        return (
          <div key={key} style={{ marginBottom: 8 }}>
            <div style={{ color: "var(--muted)" }}>{key === "custom_fields" ? "Custom fields" : "Choice sets"}:</div>
            {d.missing_on_instance.length > 0 && <div style={{ marginLeft: 12, color: "var(--danger)" }}>missing on instance: {d.missing_on_instance.join(", ")}</div>}
            {d.extra_on_instance.length > 0 && <div style={{ marginLeft: 12, color: "var(--warning)" }}>extra on instance (not in template): {d.extra_on_instance.join(", ")}</div>}
            <ChangedItemsList items={d.changed} />
          </div>
        );
      })}
    </div>
  );
}
