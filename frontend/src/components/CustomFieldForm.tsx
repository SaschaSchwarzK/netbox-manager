import { useState } from "react";
import { CONTENT_TYPE_GROUPS } from "../constants/netboxContentTypes";

const TYPES = [
  { value: "text", label: "Text" },
  { value: "longtext", label: "Long text" },
  { value: "integer", label: "Integer" },
  { value: "decimal", label: "Decimal" },
  { value: "boolean", label: "Boolean" },
  { value: "date", label: "Date" },
  { value: "datetime", label: "Date & time" },
  { value: "url", label: "URL" },
  { value: "json", label: "JSON" },
  { value: "select", label: "Selection" },
  { value: "multiselect", label: "Multiple selection" },
  { value: "object", label: "Object" },
  { value: "multiobject", label: "Multiple object" },
];
const FILTER_LOGIC = [
  { value: "disabled", label: "Disabled" },
  { value: "loose", label: "Loose" },
  { value: "exact", label: "Exact" },
];
const UI_VISIBLE = [
  { value: "always", label: "Always" },
  { value: "if-set", label: "If set" },
  { value: "hidden", label: "Hidden" },
];
const UI_EDITABLE = [
  { value: "yes", label: "Yes" },
  { value: "no", label: "No" },
  { value: "hidden", label: "Hidden" },
];

const NUMERIC_TYPES = ["integer", "decimal"];
const REGEX_TYPES = ["text", "longtext", "url"];
const CHOICE_TYPES = ["select", "multiselect"];
const OBJECT_TYPES = ["object", "multiobject"];

interface Props {
  initial: Record<string, any> | null; // null = creating new
  existingNames: string[]; // for uniqueness hint, excludes the item being edited
  choiceSetNames: string[];
  onSave: (value: Record<string, any>) => void;
  onCancel: () => void;
}

function blankField(): Record<string, any> {
  return {
    name: "", label: "", group_name: "", description: "", content_types: [], weight: 100, comments: "",
    type: "text", required: false, unique: false, default: null,
    choice_set: null, related_object_type: "", related_object_filter: null,
    filter_logic: "loose", search_weight: 1000, ui_visible: "always", ui_editable: "yes", is_cloneable: false,
    validation_minimum: null, validation_maximum: null, validation_regex: "",
  };
}

export default function CustomFieldForm({ initial, existingNames, choiceSetNames, onSave, onCancel }: Props) {
  const [value, setValue] = useState<Record<string, any>>(() => ({ ...blankField(), ...(initial ?? {}) }));
  const [contentTypeFilter, setContentTypeFilter] = useState("");
  const [customContentType, setCustomContentType] = useState("");
  const [error, setError] = useState<string | null>(null);

  const set = (key: string, v: any) => setValue((prev) => ({ ...prev, [key]: v }));

  const toggleContentType = (ct: string) => {
    const list: string[] = value.content_types ?? [];
    set("content_types", list.includes(ct) ? list.filter((c) => c !== ct) : [...list, ct]);
  };

  const addCustomContentType = () => {
    const ct = customContentType.trim();
    if (!ct) return;
    const list: string[] = value.content_types ?? [];
    if (!list.includes(ct)) set("content_types", [...list, ct]);
    setCustomContentType("");
  };

  const handleSave = () => {
    if (!value.name.trim()) { setError("Name is required."); return; }
    if (!/^[a-zA-Z0-9_]+$/.test(value.name)) { setError("Name may only contain letters, numbers, and underscores."); return; }
    if (existingNames.includes(value.name)) { setError("A custom field with this name already exists."); return; }
    if ((value.content_types ?? []).length === 0) { setError("Select at least one model this field applies to."); return; }
    if (CHOICE_TYPES.includes(value.type) && !value.choice_set) { setError("Selection fields must specify a choice set."); return; }
    if (OBJECT_TYPES.includes(value.type) && !value.related_object_type) { setError("Object fields must specify a related object type."); return; }

    const cleaned = { ...value };
    if (!CHOICE_TYPES.includes(cleaned.type)) cleaned.choice_set = null;
    if (!OBJECT_TYPES.includes(cleaned.type)) { cleaned.related_object_type = null; cleaned.related_object_filter = null; }
    if (!NUMERIC_TYPES.includes(cleaned.type)) { cleaned.validation_minimum = null; cleaned.validation_maximum = null; }
    if (!REGEX_TYPES.includes(cleaned.type)) cleaned.validation_regex = "";
    setError(null);
    onSave(cleaned);
  };

  const filteredGroups = CONTENT_TYPE_GROUPS.map((g) => ({
    ...g,
    types: g.types.filter((t) => !contentTypeFilter.trim() || t.label.toLowerCase().includes(contentTypeFilter.toLowerCase()) || t.value.includes(contentTypeFilter.toLowerCase())),
  })).filter((g) => g.types.length > 0);

  const extraSelectedContentTypes: string[] = (value.content_types ?? []).filter(
    (ct: string) => !CONTENT_TYPE_GROUPS.some((g) => g.types.some((t) => t.value === ct))
  );

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <h2>{initial ? "Edit Custom Field" : "Add Custom Field"}</h2>
        <p style={{ color: "var(--muted)", fontSize: 12.5 }}>Mirrors NetBox's own Custom Field form.</p>

        <div className="modal-section">
          <div className="modal-section-title">General</div>
          <div className="form-row">
            <label>Model(s) — object types this field applies to</label>
            <input placeholder="Filter…" value={contentTypeFilter} onChange={(e) => setContentTypeFilter(e.target.value)} style={{ marginBottom: 6 }} />
            <div className="content-type-grid">
              {filteredGroups.map((g) => (
                <div key={g.group} style={{ gridColumn: "1 / -1", marginTop: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--muted)", fontWeight: 600, marginBottom: 2 }}>{g.group}</div>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "2px 12px" }}>
                    {g.types.map((t) => (
                      <label key={t.value}>
                        <input type="checkbox" style={{ width: "auto" }} checked={(value.content_types ?? []).includes(t.value)} onChange={() => toggleContentType(t.value)} />
                        {t.label}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            {extraSelectedContentTypes.length > 0 && (
              <p style={{ fontSize: 12, marginTop: 4 }}>
                Also applied to (custom): {extraSelectedContentTypes.map((ct) => (
                  <span key={ct} className="pill" style={{ marginRight: 4 }}>
                    {ct} <a onClick={() => toggleContentType(ct)} style={{ cursor: "pointer", color: "var(--danger)" }}>×</a>
                  </span>
                ))}
              </p>
            )}
            <div className="toolbar" style={{ marginTop: 6 }}>
              <input className="mono" placeholder="app_label.model (e.g. dcim.consoleport)" value={customContentType} onChange={(e) => setCustomContentType(e.target.value)} style={{ maxWidth: 280 }} />
              <button onClick={addCustomContentType}>Add</button>
            </div>
          </div>

          <div className="field-row-2col">
            <div className="form-row">
              <label>Name</label>
              <input className="mono" value={value.name} onChange={(e) => set("name", e.target.value)} disabled={!!initial} />
              <div className="field-help">Raw field name (letters, numbers, underscores only). Used in the database and API.</div>
            </div>
            <div className="form-row">
              <label>Label</label>
              <input value={value.label ?? ""} onChange={(e) => set("label", e.target.value)} />
              <div className="field-help">Human-friendly name. Falls back to the field name if blank.</div>
            </div>
          </div>
          <div className="field-row-2col">
            <div className="form-row">
              <label>Group name</label>
              <input value={value.group_name ?? ""} onChange={(e) => set("group_name", e.target.value)} />
              <div className="field-help">Fields sharing a group are displayed together.</div>
            </div>
            <div className="form-row">
              <label>Weight</label>
              <input type="number" value={value.weight ?? 100} onChange={(e) => set("weight", parseInt(e.target.value, 10))} />
              <div className="field-help">Lower weights appear first within a group.</div>
            </div>
          </div>
          <div className="form-row">
            <label>Description</label>
            <input value={value.description ?? ""} onChange={(e) => set("description", e.target.value)} />
          </div>
          <div className="form-row">
            <label>Comments</label>
            <textarea rows={2} value={value.comments ?? ""} onChange={(e) => set("comments", e.target.value)} />
          </div>
        </div>

        <div className="modal-section">
          <div className="modal-section-title">Values</div>
          <div className="field-row-2col">
            <div className="form-row">
              <label>Type</label>
              <select value={value.type} onChange={(e) => set("type", e.target.value)}>
                {TYPES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </div>
            <div className="form-row">
              <label>Default</label>
              <input className="mono" value={value.default ?? ""} onChange={(e) => set("default", e.target.value || null)} placeholder='JSON, e.g. "foo" or 42 or true' />
            </div>
          </div>
          <div className="field-row-2col">
            <div className="form-row">
              <label><input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={!!value.required} onChange={(e) => set("required", e.target.checked)} />Required</label>
            </div>
            <div className="form-row">
              <label><input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={!!value.unique} onChange={(e) => set("unique", e.target.checked)} />Must be unique</label>
            </div>
          </div>

          {CHOICE_TYPES.includes(value.type) && (
            <div className="form-row">
              <label>Choice set</label>
              <select value={value.choice_set ?? ""} onChange={(e) => set("choice_set", e.target.value || null)}>
                <option value="">— select a choice set —</option>
                {choiceSetNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
              <div className="field-help">Selection fields must designate a choice set (see the Choice Sets tab).</div>
            </div>
          )}

          {OBJECT_TYPES.includes(value.type) && (
            <>
              <div className="form-row">
                <label>Related object type</label>
                <input className="mono" value={value.related_object_type ?? ""} onChange={(e) => set("related_object_type", e.target.value)} placeholder="e.g. dcim.device" />
                <div className="field-help">The NetBox object type this field references.</div>
              </div>
              <div className="form-row">
                <label>Related object filter (JSON, optional)</label>
                <input className="mono" value={value.related_object_filter ? JSON.stringify(value.related_object_filter) : ""}
                  onChange={(e) => { try { set("related_object_filter", e.target.value ? JSON.parse(e.target.value) : null); } catch { /* ignore until valid */ } }}
                  placeholder='e.g. {"status": "active"}' />
              </div>
            </>
          )}
        </div>

        <div className="modal-section">
          <div className="modal-section-title">Behavior</div>
          <div className="field-row-3col">
            <div className="form-row">
              <label>Filter logic</label>
              <select value={value.filter_logic ?? "loose"} onChange={(e) => set("filter_logic", e.target.value)}>
                {FILTER_LOGIC.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
            <div className="form-row">
              <label>UI visible</label>
              <select value={value.ui_visible ?? "always"} onChange={(e) => set("ui_visible", e.target.value)}>
                {UI_VISIBLE.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
            <div className="form-row">
              <label>UI editable</label>
              <select value={value.ui_editable ?? "yes"} onChange={(e) => set("ui_editable", e.target.value)}>
                {UI_EDITABLE.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </div>
          </div>
          <div className="field-row-2col">
            <div className="form-row">
              <label>Search weight</label>
              <input type="number" value={value.search_weight ?? 1000} onChange={(e) => set("search_weight", parseInt(e.target.value, 10))} />
              <div className="field-help">Lower is more important; 0 disables search indexing.</div>
            </div>
            <div className="form-row">
              <label><input type="checkbox" style={{ width: "auto", marginRight: 6 }} checked={!!value.is_cloneable} onChange={(e) => set("is_cloneable", e.target.checked)} />Is cloneable</label>
              <div className="field-help">Replicate this value when cloning objects.</div>
            </div>
          </div>
        </div>

        {(NUMERIC_TYPES.includes(value.type) || REGEX_TYPES.includes(value.type)) && (
          <div className="modal-section">
            <div className="modal-section-title">Validation Rules</div>
            {NUMERIC_TYPES.includes(value.type) && (
              <div className="field-row-2col">
                <div className="form-row">
                  <label>Minimum value</label>
                  <input type="number" value={value.validation_minimum ?? ""} onChange={(e) => set("validation_minimum", e.target.value === "" ? null : parseInt(e.target.value, 10))} />
                </div>
                <div className="form-row">
                  <label>Maximum value</label>
                  <input type="number" value={value.validation_maximum ?? ""} onChange={(e) => set("validation_maximum", e.target.value === "" ? null : parseInt(e.target.value, 10))} />
                </div>
              </div>
            )}
            {REGEX_TYPES.includes(value.type) && (
              <div className="form-row">
                <label>Validation regex</label>
                <input className="mono" value={value.validation_regex ?? ""} onChange={(e) => set("validation_regex", e.target.value)} placeholder="^[A-Z]{3}$" />
              </div>
            )}
          </div>
        )}

        {error && <p style={{ color: "var(--danger)", fontSize: 13, marginTop: 12 }}>{error}</p>}
        <div className="toolbar" style={{ marginTop: 18 }}>
          <button className="primary" onClick={handleSave}>Save</button>
          <button onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
