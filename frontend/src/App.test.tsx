import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";

function json(payload: unknown) {
  return Promise.resolve(new Response(JSON.stringify(payload), {
    status: 200, headers: { "Content-Type": "application/json" },
  }));
}

describe("App", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("shows login when the auth endpoint reports a logged-out user", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => json({
      auth_enabled: true, oidc_enabled: true, local_login_enabled: false,
      authenticated: false, user: null, role: "viewer", app_admin: false,
    })));
    render(<App />);
    expect(await screen.findByText("Sign in with SSO")).toBeTruthy();
  });

  it("resolves an authenticated lazy route", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/auth/me")) return json({
        auth_enabled: true, authenticated: true, oidc_enabled: false, local_login_enabled: false,
        user: { name: "Tester", groups: [] }, role: "admin", app_admin: true,
      });
      return json([]);
    }));
    render(<App />);
    expect(await screen.findByText("Search")).toBeTruthy();
    expect(await screen.findByText("Tester")).toBeTruthy();
  });
});
