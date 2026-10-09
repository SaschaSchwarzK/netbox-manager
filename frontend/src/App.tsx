import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import LoginScreen from "./pages/LoginScreen";
import { authApi, AuthMeResponse } from "./api/client";
import { AccessContext, makeAccessValue } from "./contexts/AccessContext";

const InstancesPage = lazy(() => import("./pages/InstancesPage"));
const DeviceTypesPage = lazy(() => import("./pages/DeviceTypesPage"));
const ModuleTypesPage = lazy(() => import("./pages/ModuleTypesPage"));
const RackTypesPage = lazy(() => import("./pages/RackTypesPage"));
const CustomFieldsPage = lazy(() => import("./pages/CustomFieldsPage"));
const ReferenceDataPage = lazy(() => import("./pages/ReferenceDataPage"));
const BulkImportPage = lazy(() => import("./pages/BulkImportPage"));
const DeviceTypeEditorPage = lazy(() => import("./pages/DeviceTypeEditor"));
const ModuleTypeEditorPage = lazy(() => import("./pages/ModuleTypeEditor"));
const RackTypeEditorPage = lazy(() => import("./pages/RackTypeEditor"));
const GithubTargetsPage = lazy(() => import("./pages/GithubTargetsPage"));
const SearchPage = lazy(() => import("./pages/SearchPage"));
const DriftPage = lazy(() => import("./pages/DriftPage"));
const AuditLogPage = lazy(() => import("./pages/AuditLogPage"));
const SyslogPage = lazy(() => import("./pages/SyslogPage"));
const FleetPage = lazy(() => import("./pages/FleetPage"));
const AccessControlPage = lazy(() => import("./pages/AccessControlPage"));
const TenantPermissionsPage = lazy(() => import("./pages/TenantPermissionsPage"));
const MigrationPage = lazy(() => import("./pages/MigrationPage"));
const ExportPage = lazy(() => import("./pages/ExportPage"));

export default function App() {
  const [me, setMe] = useState<AuthMeResponse | null>(null);

  useEffect(() => {
    const loggedOut = () => setMe({
      auth_enabled: true,
      oidc_enabled: false,
      local_login_enabled: false,
      authenticated: false,
      user: null,
      role: "viewer",
      app_admin: false,
    });
    authApi.me().then(setMe).catch(loggedOut);
    window.addEventListener("nbm:unauthorized", loggedOut);
    return () => window.removeEventListener("nbm:unauthorized", loggedOut);
  }, []);

  if (!me) return null; // brief flash while the initial /api/auth/me check resolves

  if (me.auth_enabled && !me.authenticated) {
    return <LoginScreen auth={me} />;
  }

  const access = makeAccessValue(me.role, me.app_admin);

  const handleLogout = async () => {
    await authApi.logout();
    window.location.href = "/";
  };

  return (
    <AccessContext.Provider value={access}>
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
            <NavLink to="/module-types" className={({ isActive }) => (isActive ? "active" : "")}>
              Module Types
            </NavLink>
            <NavLink to="/rack-types" className={({ isActive }) => (isActive ? "active" : "")}>
              Rack Types
            </NavLink>
            <NavLink to="/custom-fields" className={({ isActive }) => (isActive ? "active" : "")}>
              Custom Fields
            </NavLink>
            <NavLink to="/reference-data" className={({ isActive }) => (isActive ? "active" : "")}>
              Reference Data
            </NavLink>
            <NavLink to="/tenant-permissions" className={({ isActive }) => (isActive ? "active" : "")}>
              Tenant Permissions
            </NavLink>
            <NavLink to="/bulk-import" className={({ isActive }) => (isActive ? "active" : "")}>
              Bulk Import
            </NavLink>
            <NavLink to="/migration" className={({ isActive }) => (isActive ? "active" : "")}>
              Data Migration
            </NavLink>
            <NavLink to="/export" className={({ isActive }) => (isActive ? "active" : "")}>
              Data Export
            </NavLink>
            <NavLink to="/drift" className={({ isActive }) => (isActive ? "active" : "")}>
              Drift
            </NavLink>
            <NavLink to="/audit" className={({ isActive }) => (isActive ? "active" : "")}>
              Audit Log
            </NavLink>
            <NavLink to="/syslog" className={({ isActive }) => (isActive ? "active" : "")}>
              Syslog
            </NavLink>
            <NavLink to="/instances" className={({ isActive }) => (isActive ? "active" : "")}>
              NetBox Instances
            </NavLink>
            <NavLink to="/github-targets" className={({ isActive }) => (isActive ? "active" : "")}>
              GitHub Targets
            </NavLink>
            {access.appAdmin && (
              <NavLink to="/access-control" className={({ isActive }) => (isActive ? "active" : "")}>
                Access Control
              </NavLink>
            )}
          </nav>
          {me.user && (
            <div style={{ marginTop: "auto", padding: "12px 20px", borderTop: "1px solid var(--border)", fontSize: 12 }}>
              <div style={{ color: "var(--text)" }}>{me.user.name ?? me.user.email}</div>
              <div style={{ color: "var(--muted)", marginTop: 2 }}>role: {me.role}</div>
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
          <Suspense fallback={<div className="card">Loading…</div>}>
          <Routes>
            <Route path="/" element={<SearchPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/fleet" element={<FleetPage />} />
            <Route path="/device-types" element={<DeviceTypesPage />} />
            <Route path="/device-types/:targetId/edit" element={<DeviceTypeEditorPage />} />
            <Route path="/module-types" element={<ModuleTypesPage />} />
            <Route path="/module-types/:targetId/edit" element={<ModuleTypeEditorPage />} />
            <Route path="/rack-types" element={<RackTypesPage />} />
            <Route path="/rack-types/:targetId/edit" element={<RackTypeEditorPage />} />
            <Route path="/custom-fields" element={<CustomFieldsPage />} />
            <Route path="/reference-data" element={<ReferenceDataPage />} />
            <Route path="/tenant-permissions" element={<TenantPermissionsPage />} />
            <Route path="/bulk-import" element={<BulkImportPage />} />
            <Route path="/migration" element={<MigrationPage />} />
            <Route path="/export" element={<ExportPage />} />
            <Route path="/drift" element={<DriftPage />} />
            <Route path="/audit" element={<AuditLogPage />} />
            <Route path="/syslog" element={<SyslogPage />} />
            <Route path="/instances" element={<InstancesPage />} />
            <Route path="/github-targets" element={<GithubTargetsPage />} />
            {access.appAdmin && <Route path="/access-control" element={<AccessControlPage />} />}
          </Routes>
          </Suspense>
        </main>
      </div>
    </BrowserRouter>
    </AccessContext.Provider>
  );
}
