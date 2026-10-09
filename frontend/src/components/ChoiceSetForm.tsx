import { useState } from "react";

const BASE_CHOICES = [
  { value: "", label: "— none —" },
  { value: "IATA", label: "IATA airport codes" },
  { value: "ISO_3166", label: "ISO 3166 — two-letter country codes" },
  { value: "UN_LOCODE", label: "UN/LOCODE — five-character location identifiers" },
];

interface Props {
  initial: Record<string, any> | null;
  existingNames: string[];
  onSave: (value: Record<string, any>) => void;
  onCancel: () => void;
}

function blankChoiceSet(): Record<string, any> {
  return { name: "", description: "", base_choices: "", extra_choices: [], order_alphabetically: false };
}

export default function ChoiceSetForm({ initial, existingNames, onSave, onCancel }: Props) {
  const [value, setValue] = useState<Record<string, any>>(() => ({ ...blankChoiceSet(), ...(initial ?? {}) }));
  const [error, setError] = useState<string | null>(null);

  const set = (key: string, v: any) => setValue((prev) => ({ ...prev, [key]: v }));

  const choices: string[][] = value.extra_choices ?? [];
  const updateChoice = (idx: number, col: 0 | 1, v: string) => {
    const next = choices.map((c) => [...c]);
    next[idx][col] = v;
    set("extra_choices", next);
  };
  const addChoice = () => set("extra_choices", [...choices, ["", ""]]);
  const removeChoice = (idx: number) => set("extra_choices", choices.filter((_, i) => i !== idx));

  const handleSave = () => {
    if (!value.name.trim()) { setError("Name is required."); return; }
    if (existingNames.includes(value.name)) { setError("A choice set with this name already exists."); return; }
    const cleanChoices = choices.filter((c) => c[0]?.trim());
    if (!value.base_choices && cleanChoices.length < 2) {
      setError("Without a base choice set, at least two extra choices are required.");
      return;
    }
    const values = cleanChoices.map((c) => c[0]);
    if (new Set(values).size !== values.length) { setError("Choice values must be unique."); return; }

    setError(null);
    onSave({ ...value, base_choices: value.base_choices || null, extra_choices: cleanChoices });
  };

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <h2>{initial ? "Edit Choice Set" : "Add Choice Set"}</h2>
        <p style={{ color: "var(--muted)", fontSize: 12.5 }}>Mirrors NetBox's own Custom Field Choice Set form.</p>

        <div className="modal-section">
          <div className="field-row-2col">
            <div className="form-row">
              <label>Name</label>
              <input className="mono" value={value.name} onChange={(e) => set("name", e.target.value)} disabled={!!initial} />
            </div>
            <div className="form-row">
              <label>Base choices</label>
              <select value={value.base_choices ?? ""} onChange={(e) => set("base_choices", e.target.value)}>
                {BASE_CHOICES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
              <div className="field-help">A predefined set to include, optional. Extra choices below are appended to it.</div>
            </div>
          </div>
          <div className="form-row">
            <label>Description</label>
            <input value={value.description ?? ""} onChange={(e) => set("description", e.target.value)} />
          </div>
          <div className="form-row">
            <label><input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={!!value.order_alphabetically} onChange={(e) => set("order_alphabetically", e.target.checked)} />Order alphabetically</label>
          </div>
        </div>

        <div className="modal-section">
          <div className="modal-section-title">Extra Choices</div>
          {choices.length === 0 && <p style={{ color: "var(--muted)", fontSize: 12.5 }}>No extra choices yet.</p>}
          {choices.map((c, idx) => (
            <div className="choice-row" key={idx}>
              <input className="mono" placeholder="value" value={c[0] ?? ""} onChange={(e) => updateChoice(idx, 0, e.target.value)} />
              <input placeholder="label" value={c[1] ?? ""} onChange={(e) => updateChoice(idx, 1, e.target.value)} />
              <button className="danger" onClick={() => removeChoice(idx)} style={{ padding: "4px 10px" }}>×</button>
            </div>
          ))}
          <button onClick={addChoice}>+ Add choice</button>
        </div>

        {error && <p style={{ color: "var(--danger)", fontSize: 13, marginTop: 12 }}>{error}</p>}
        <div className="toolbar" style={{ marginTop: 18 }}>
          <button className="primary" onClick={handleSave}>Save</button>
          <button onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
