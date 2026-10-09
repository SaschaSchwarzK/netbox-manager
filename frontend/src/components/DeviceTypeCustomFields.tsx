import { useEffect, useState } from "react";
import { customFieldsApi } from "../api/client";

const DEVICE_TYPE_CONTENT_TYPE = "dcim.devicetype";

interface CustomFieldDef {
  name: string;
  label?: string | null;
  type: string;
  object_types: string[];
  description?: string | null;
  required?: boolean;
  default?: any;
  choice_set?: string | null;
  related_object_type?: string | null;
}

interface ChoiceSetDef {
  name: string;
  extra_choices: string[][]; // [value, label][]
  base_choices?: string | null;
}

interface Props {
  targetId: string;
  values: Record<string, any>;
  onChange: (values: Record<string, any>) => void;
}

function setValue(values: Record<string, any>, name: string, value: any): Record<string, any> {
  const next = { ...values };
  if (value === "" || value === null || value === undefined) {
    delete next[name];
  } else {
    next[name] = value;
  }
  return next;
}

function FieldInput({ def, choiceSets, value, onSet }: {
  def: CustomFieldDef; choiceSets: Record<string, ChoiceSetDef>; value: any; onSet: (v: any) => void;
}) {
  // Declared unconditionally (only used by the "json" case below) so hook
  // order stays stable regardless of which branch of the switch renders.
  const [jsonText, setJsonText] = useState<string | null>(null);
  const [jsonErr, setJsonErr] = useState<string | null>(null);

  switch (def.type) {
    case "boolean":
      return (
        <label>
          <input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={!!value} onChange={(e) => onSet(e.target.checked)} />
          {value ? "True" : "False"}
        </label>
      );
    case "integer":
      return <input type="number" step="1" value={value ?? ""} onChange={(e) => onSet(e.target.value === "" ? null : parseInt(e.target.value, 10))} />;
    case "decimal":
      return <input type="number" step="any" value={value ?? ""} onChange={(e) => onSet(e.target.value === "" ? null : parseFloat(e.target.value))} />;
    case "date":
      return <input type="date" value={value ?? ""} onChange={(e) => onSet(e.target.value || null)} />;
    case "datetime":
      return <input type="datetime-local" value={value ?? ""} onChange={(e) => onSet(e.target.value || null)} />;
    case "longtext":
      return <textarea rows={3} value={value ?? ""} onChange={(e) => onSet(e.target.value)} />;
    case "url":
      return <input type="url" className="mono" value={value ?? ""} onChange={(e) => onSet(e.target.value)} />;
    case "json": {
      const text = jsonText ?? (value !== undefined && value !== null ? JSON.stringify(value) : "");
      return (
        <div>
          <input
            className="mono"
            value={text}
            onChange={(e) => {
              setJsonText(e.target.value);
              if (e.target.value.trim() === "") { setJsonErr(null); onSet(null); return; }
              try { onSet(JSON.parse(e.target.value)); setJsonErr(null); } catch { setJsonErr("Invalid JSON"); }
            }}
            placeholder='e.g. {"key": "value"} or [1, 2]'
          />
          {jsonErr && <div className="field-help" style={{ color: "var(--danger)" }}>{jsonErr}</div>}
        </div>
      );
    }
    case "select": {
      const cs = def.choice_set ? choiceSets[def.choice_set] : undefined;
      if (!cs || cs.extra_choices.length === 0) {
        return <input className="mono" value={value ?? ""} onChange={(e) => onSet(e.target.value)}
          placeholder={cs?.base_choices ? `value from NetBox's "${cs.base_choices}" set` : "value"} />;
      }
      return (
        <select value={value ?? ""} onChange={(e) => onSet(e.target.value || null)}>
          <option value="">—</option>
          {cs.extra_choices.map(([v, label]) => <option key={v} value={v}>{label || v}</option>)}
        </select>
      );
    }
    case "multiselect": {
      const cs = def.choice_set ? choiceSets[def.choice_set] : undefined;
      const list: string[] = Array.isArray(value) ? value : [];
      const toggle = (v: string) => onSet(list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
      if (!cs || cs.extra_choices.length === 0) {
        return (
          <input className="mono" value={list.join(",")} placeholder="comma,separated,values"
            onChange={(e) => onSet(e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} />
        );
      }
      return (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "2px 12px" }}>
          {cs.extra_choices.map(([v, label]) => (
            <label key={v} style={{ width: "auto" }}>
              <input type="checkbox" style={{ width: "auto", marginRight: 4 }} checked={list.includes(v)} onChange={() => toggle(v)} />
              {label || v}
            </label>
          ))}
        </div>
      );
    }
    case "object":
    case "multiobject":
      return (
        <div>
          <input className="mono" value={value ?? ""} onChange={(e) => onSet(e.target.value || null)} placeholder="Object ID (or IDs, comma-separated)" />
          <div className="field-help">
            {def.type === "multiobject" ? "Comma-separated primary keys" : "Primary key"} of the related {def.related_object_type ?? "object"} — NetBox Manager doesn't resolve object references, so use the numeric ID.
          </div>
        </div>
      );
    default: // text
      return <input value={value ?? ""} onChange={(e) => onSet(e.target.value)} />;
  }
}

export default function DeviceTypeCustomFields({ targetId, values, onChange }: Props) {
  const [fields, setFields] = useState<CustomFieldDef[] | null>(null);
  const [choiceSets, setChoiceSets] = useState<Record<string, ChoiceSetDef>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    customFieldsApi.get(targetId).then((file) => {
      if (cancelled) return;
      const all = (file.payload.custom_fields ?? []) as CustomFieldDef[];
      setFields(all.filter((f) => (f.object_types ?? []).includes(DEVICE_TYPE_CONTENT_TYPE)));
      const csMap: Record<string, ChoiceSetDef> = {};
      ((file.payload.custom_field_choice_sets ?? []) as ChoiceSetDef[]).forEach((cs) => { csMap[cs.name] = cs; });
      setChoiceSets(csMap);
    }).catch((err) => !cancelled && setError(err.message ?? "Could not load the custom-fields template."));
    return () => { cancelled = true; };
  }, [targetId]);

  if (error) return <p style={{ color: "var(--danger)", fontSize: 13 }}>{error}</p>;
  if (fields === null) return <p style={{ color: "var(--muted)" }}>Loading custom-field definitions…</p>;

  // Values already on this device type but whose definition either isn't in the
  // template or isn't scoped to device types (e.g. removed since, or edited by
  // hand) — still shown, read/write, so nothing silently disappears.
  const knownNames = new Set(fields.map((f) => f.name));
  const orphanNames = Object.keys(values).filter((n) => !knownNames.has(n));

  return (
    <div className="card">
      <h2>Custom Fields</h2>
      <p style={{ color: "var(--muted)", fontSize: 13, marginTop: -6 }}>
        Values for custom fields scoped to "Device type" ({DEVICE_TYPE_CONTENT_TYPE}) in this repo's custom-fields template.
        Defined and managed under Custom Fields.
      </p>
      {fields.length === 0 && orphanNames.length === 0 && (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>
          No custom fields are scoped to device types yet — add one under Custom Fields and apply it to "Device type".
        </p>
      )}
      {fields.map((f) => (
        <div className="form-row" key={f.name}>
          <label title={f.description ?? undefined}>
            {f.label || f.name} {f.required && <span style={{ color: "var(--danger)" }}>*</span>}
          </label>
          <FieldInput def={f} choiceSets={choiceSets} value={values[f.name]} onSet={(v) => onChange(setValue(values, f.name, v))} />
          {f.description && <div className="field-help">{f.description}</div>}
        </div>
      ))}
      {orphanNames.length > 0 && (
        <>
          <div className="modal-section-title" style={{ marginTop: fields.length ? 14 : 0 }}>
            Other values on this device type (no matching definition for "Device type" found in the template)
          </div>
          {orphanNames.map((name) => (
            <div className="form-row" key={name}>
              <label className="mono">{name}</label>
              <input className="mono" value={typeof values[name] === "string" ? values[name] : JSON.stringify(values[name])}
                onChange={(e) => onChange(setValue(values, name, e.target.value))} />
            </div>
          ))}
        </>
      )}
    </div>
  );
}
