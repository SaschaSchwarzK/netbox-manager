import { useState } from "react";

export interface FieldDef {
  key: string;
  label: string;
  type?: "text" | "checkbox" | "number" | "select" | "csv" | "pairs";
  options?: string[];
  width?: string;
}

interface Props {
  rows: Record<string, any>[];
  fields: FieldDef[];
  onChange: (rows: Record<string, any>[]) => void;
  namePattern?: boolean; // show a bulk-add "name{1-N}" helper
}

export default function ComponentGrid({ rows, fields, onChange, namePattern }: Props) {
  const [bulk, setBulk] = useState("");

  const updateRow = (idx: number, key: string, value: any) => {
    const next = rows.slice();
    next[idx] = { ...next[idx], [key]: value };
    onChange(next);
  };

  const addRow = () => {
    const blank: Record<string, any> = {};
    fields.forEach((f) => (blank[f.key] = f.type === "checkbox" ? false : f.type === "csv" || f.type === "pairs" ? [] : ""));
    onChange([...rows, blank]);
  };

  const removeRow = (idx: number) => {
    onChange(rows.filter((_, i) => i !== idx));
  };

  const expandBulkPattern = (pattern: string): string[] => {
    // Expands e.g. "GigabitEthernet1/0/{1-48}" into 48 names.
    const match = pattern.match(/\{(\d+)-(\d+)\}/);
    if (!match) return [pattern];
    const [full, startStr, endStr] = match;
    const start = parseInt(startStr, 10);
    const end = parseInt(endStr, 10);
    const names: string[] = [];
    for (let i = start; i <= end; i++) {
      names.push(pattern.replace(full, String(i)));
    }
    return names;
  };

  const handleBulkAdd = () => {
    if (!bulk.trim()) return;
    const names = expandBulkPattern(bulk.trim());
    const newRows = names.map((name) => {
      const blank: Record<string, any> = { name };
      fields.forEach((f) => {
        if (f.key !== "name") blank[f.key] = f.type === "checkbox" ? false : (f.options?.[0] ?? "");
      });
      return blank;
    });
    onChange([...rows, ...newRows]);
    setBulk("");
  };

  return (
    <div>
      {namePattern && (
        <div className="toolbar">
          <input
            className="mono"
            placeholder="e.g. GigabitEthernet1/0/{1-48}"
            value={bulk}
            onChange={(e) => setBulk(e.target.value)}
            style={{ maxWidth: 320 }}
          />
          <button onClick={handleBulkAdd}>Bulk add</button>
        </div>
      )}
      <table className="grid-editor">
        <thead>
          <tr>
            {fields.map((f) => (
              <th key={f.key} style={{ width: f.width }}>{f.label}</th>
            ))}
            <th className="row-actions"></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => (
            <tr key={idx}>
              {fields.map((f) => (
                <td key={f.key}>
                  {f.type === "checkbox" ? (
                    <input
                      type="checkbox"
                      style={{ width: "auto" }}
                      checked={!!row[f.key]}
                      onChange={(e) => updateRow(idx, f.key, e.target.checked)}
                    />
                  ) : f.type === "select" ? (
                    <select value={row[f.key] ?? ""} onChange={(e) => updateRow(idx, f.key, e.target.value)}>
                      {(f.options ?? []).map((opt) => (
                        <option key={opt} value={opt}>{opt}</option>
                      ))}
                    </select>
                  ) : f.type === "csv" ? (
                    <input
                      className="mono"
                      type="text"
                      placeholder="comma, separated, values"
                      defaultValue={(row[f.key] ?? []).join(", ")}
                      onBlur={(e) => updateRow(idx, f.key, e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
                    />
                  ) : f.type === "pairs" ? (
                    <textarea
                      className="mono"
                      rows={3}
                      placeholder={"value=Label\nvalue2=Label 2"}
                      defaultValue={(row[f.key] ?? []).map((pair: string[]) => pair.join("=")).join("\n")}
                      onBlur={(e) => updateRow(
                        idx, f.key,
                        e.target.value.split("\n").map((l) => l.trim()).filter(Boolean)
                          .map((l) => l.includes("=") ? l.split(/=(.*)/s).slice(0, 2).map((s) => s.trim()) : [l, l])
                      )}
                    />
                  ) : (
                    <input
                      className="mono"
                      type={f.type === "number" ? "number" : "text"}
                      value={row[f.key] ?? ""}
                      onChange={(e) => updateRow(idx, f.key, e.target.value)}
                    />
                  )}
                </td>
              ))}
              <td className="row-actions">
                <button className="danger" onClick={() => removeRow(idx)} style={{ padding: "2px 8px" }}>×</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={fields.length + 1} style={{ color: "var(--muted)" }}>No rows yet.</td></tr>
          )}
        </tbody>
      </table>
      <div style={{ marginTop: 8 }}>
        <button onClick={addRow}>+ Add row</button>
      </div>
    </div>
  );
}
