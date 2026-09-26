import { useEffect, useState } from "react";
import { instancesApi, NetboxInstance, searchApi, InstanceSearchResult, SearchResultItem } from "../api/client";

function ResultTable({
  title, rows, col2Label = "Serial", typeLabel = "Type", col4Label = "Site",
}: { title: string; rows: SearchResultItem[]; col2Label?: string; typeLabel?: string; col4Label?: string }) {
  if (rows.length === 0) return null;
  return (
    <div style={{ marginBottom: 14 }}>
      <p style={{ fontSize: 12, color: "var(--muted)", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: 6 }}>
        {title} ({rows.length})
      </p>
      <table>
        <thead>
          <tr><th>Name</th><th>{col2Label}</th><th>{typeLabel}</th><th>{col4Label}</th><th>Status</th><th></th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.name}</td>
              <td className="mono">{r.serial ?? "—"}</td>
              <td>{r.type_display ?? "—"}</td>
              <td>{r.site ?? "—"}</td>
              <td>{r.status ?? "—"}</td>
              <td><a href={r.url} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>Open in NetBox</a></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function SearchPage() {
  const [instances, setInstances] = useState<NetboxInstance[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<InstanceSearchResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    instancesApi.list().then((list) => { setInstances(list); setSelected(list.map((i) => i.id)); });
  }, []);

  const toggleInstance = (id: string) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const handleSearch = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await searchApi.search(query.trim(), selected);
      setResults(resp.results);
    } catch (err: any) {
      setError(err.message ?? "Search failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h1>Search</h1>
      <p className="page-subtitle">Find devices, virtual machines, virtual device contexts, IP addresses, prefixes, and MAC addresses across every selected NetBox instance at once.</p>

      <div className="card">
        <div className="toolbar">
          <input
            style={{ maxWidth: 360 }}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") handleSearch(); }}
            placeholder="Name, serial, IP/prefix, or MAC address…"
          />
          <button className="primary" disabled={!query.trim() || selected.length === 0 || loading} onClick={handleSearch}>
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
        <p style={{ fontSize: 12, color: "var(--muted)", marginBottom: 6 }}>Instances to search:</p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px" }}>
          {instances.map((inst) => (
            <label key={inst.id} style={{ fontSize: 13 }}>
              <input
                type="checkbox" style={{ width: "auto", marginRight: 6 }}
                checked={selected.includes(inst.id)}
                onChange={() => toggleInstance(inst.id)}
              />
              {inst.name}
            </label>
          ))}
          {instances.length === 0 && <span style={{ color: "var(--muted)", fontSize: 13 }}>No instances configured yet.</span>}
        </div>
      </div>

      {error && <p style={{ color: "var(--danger)", fontSize: 13 }}>{error}</p>}

      {results && results.map((r) => {
        const total = r.devices.length + r.virtual_machines.length + r.virtual_device_contexts.length
          + r.ip_addresses.length + r.prefixes.length + r.mac_addresses.length;
        return (
          <div className="card" key={r.instance_id}>
            <h2>{r.instance_name} {!r.error && <span className="pill">{total} match{total === 1 ? "" : "es"}</span>}</h2>
            {r.error ? (
              <p style={{ color: "var(--danger)", fontSize: 13 }}>Could not search this instance: {r.error}</p>
            ) : total === 0 ? (
              <p style={{ color: "var(--muted)", fontSize: 13 }}>No matches.</p>
            ) : (
              <>
                <ResultTable title="Devices" rows={r.devices} />
                <ResultTable title="Virtual Machines" rows={r.virtual_machines} />
                <ResultTable title="Virtual Device Contexts" rows={r.virtual_device_contexts} />
                <ResultTable title="IP Addresses" rows={r.ip_addresses} col2Label="—" typeLabel="Assigned To" col4Label="—" />
                <ResultTable title="Prefixes" rows={r.prefixes} col2Label="—" typeLabel="Role" col4Label="Site" />
                <ResultTable title="MAC Addresses" rows={r.mac_addresses} col2Label="—" typeLabel="Assigned To" col4Label="—" />
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
