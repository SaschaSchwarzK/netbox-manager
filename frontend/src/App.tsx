import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import InstancesPage from "./pages/InstancesPage";
import DeviceTypesPage from "./pages/DeviceTypesPage";
import CustomFieldsPage from "./pages/CustomFieldsPage";
import BulkImportPage from "./pages/BulkImportPage";
import DeviceTypeEditorPage from "./pages/DeviceTypeEditor";
import GithubTargetsPage from "./pages/GithubTargetsPage";
import SearchPage from "./pages/SearchPage";
import DriftPage from "./pages/DriftPage";
import AuditLogPage from "./pages/AuditLogPage";
import FleetPage from "./pages/FleetPage";
import LoginScreen from "./pages/LoginScreen";
import { authApi, AuthMeResponse } from "./api/client";

export default function App() {
  const [me, setMe] = useState<AuthMeResponse | null>(null);

  useEffect(() => {
    authApi.me().then(setMe).catch(() => setMe({ auth_enabled: true, authenticated: false, user: null }));
  }, []);

  if (!me) return null; // brief flash while the initial /api/auth/me check resolves

  if (me.auth_enabled && !me.authenticated) {
    return <LoginScreen />;
  }

  const handleLogout = async () => {
    await authApi.logout();
    window.location.href = "/";
  };

  return (
    <BrowserRouter>
      <div className="app-shell">
        <aside className="sidebar">
          <div className="sidebar-brand">
            netbox<span>manager</span>
          </div>
          <nav>
            <NavLink to="/search" className={({ isActive }) => (isActive ? "active" : "")}>
              Search
            </NavLink>
            <NavLink to="/fleet" className={({ isActive }) => (isActive ? "active" : "")}>
              Fleet
            </NavLink>
            <NavLink to="/device-types" className={({ isActive }) => (isActive ? "active" : "")}>
              Device Types
            </NavLink>
            <NavLink to="/custom-fields" className={({ isActive }) => (isActive ? "active" : "")}>
              Custom Fields
            </NavLink>
            <NavLink to="/bulk-import" className={({ isActive }) => (isActive ? "active" : "")}>
              Bulk Import
            </NavLink>
            <NavLink to="/drift" className={({ isActive }) => (isActive ? "active" : "")}>
              Drift
            </NavLink>
            <NavLink to="/audit" className={({ isActive }) => (isActive ? "active" : "")}>
              Audit Log
            </NavLink>
            <NavLink to="/instances" className={({ isActive }) => (isActive ? "active" : "")}>
              NetBox Instances
            </NavLink>
            <NavLink to="/github-targets" className={({ isActive }) => (isActive ? "active" : "")}>
              GitHub Targets
            </NavLink>
          </nav>
          {me.user && (
            <div style={{ marginTop: "auto", padding: "12px 20px", borderTop: "1px solid var(--border)", fontSize: 12 }}>
              <div style={{ color: "var(--text)" }}>{me.user.name ?? me.user.email}</div>
              {me.user.groups.length > 0 && (
                <div style={{ color: "var(--muted)", marginTop: 2 }}>{me.user.groups.join(", ")}</div>
              )}
              <button onClick={handleLogout} style={{ marginTop: 8, width: "100%", padding: "4px 8px", fontSize: 12 }}>
                Sign out
              </button>
            </div>
          )}
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<SearchPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/fleet" element={<FleetPage />} />
            <Route path="/device-types" element={<DeviceTypesPage />} />
            <Route path="/device-types/:targetId/edit" element={<DeviceTypeEditorPage />} />
            <Route path="/custom-fields" element={<CustomFieldsPage />} />
            <Route path="/bulk-import" element={<BulkImportPage />} />
            <Route path="/drift" element={<DriftPage />} />
            <Route path="/audit" element={<AuditLogPage />} />
            <Route path="/instances" element={<InstancesPage />} />
            <Route path="/github-targets" element={<GithubTargetsPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
