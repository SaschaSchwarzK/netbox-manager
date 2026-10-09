import { useEffect, useState } from "react";
import { syslogApi, SyslogSettings } from "../api/client";

export default function SyslogPage() {
  const [settings, setSettings] = useState<SyslogSettings | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(null);

  useEffect(() => { syslogApi.getSettings().then(setSettings); }, []);

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await syslogApi.test();
      setTestResult(result);
    } finally {
      setTesting(false);
    }
  };

  if (!settings) return <p>Loading…</p>;

  return (
    <div>
      <h1>Syslog</h1>
      <p className="page-subtitle">
        The audit log — every GitHub save and NetBox push — is forwarded as a structured RFC 5424 syslog message
        whenever this is enabled. These settings come from the server's config file only; they can't be changed here.
      </p>

      <div className="card">
        <h2>Current configuration</h2>
        <table>
          <tbody>
            <tr><th style={{ width: 160 }}>Status</th><td>
              <span className={`status-dot ${settings.enabled ? "status-ok" : "status-error"}`} />
              {settings.enabled ? "Enabled" : "Disabled"}
            </td></tr>
            <tr><th>Protocol</th><td className="mono">{settings.protocol.toUpperCase()}</td></tr>
            <tr><th>Remote host</th><td className="mono">{settings.host || "—"}</td></tr>
            <tr><th>Remote port</th><td className="mono">{settings.port}</td></tr>
            <tr><th>Facility</th><td className="mono">{settings.facility}</td></tr>
            <tr><th>App name</th><td className="mono">{settings.app_name}</td></tr>
          </tbody>
        </table>
        <p style={{ color: "var(--muted)", fontSize: 12, marginTop: 10 }}>
          To change these, set NBM_SYSLOG_ENABLED / NBM_SYSLOG_PROTOCOL / NBM_SYSLOG_HOST / NBM_SYSLOG_PORT /
          NBM_SYSLOG_FACILITY / NBM_SYSLOG_APP_NAME in the backend's environment and restart it.
        </p>
      </div>

      <div className="card">
        <h2>Test</h2>
        <p style={{ color: "var(--muted)", fontSize: 13, marginTop: -6 }}>
          Sends one RFC 5424 test message to the configured remote host, using the settings above.
        </p>
        <button className="primary" disabled={testing} onClick={handleTest}>
          {testing ? "Sending…" : "Send test message"}
        </button>
        {testResult && (
          <p style={{ fontSize: 13, marginTop: 10 }}>
            <span className={`status-dot ${testResult.ok ? "status-ok" : "status-error"}`} />
            {testResult.detail}
          </p>
        )}
      </div>
    </div>
  );
}
