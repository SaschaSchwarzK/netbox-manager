import { render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import CustomFieldsPage from "./CustomFieldsPage";

const mocks = vi.hoisted(() => ({
  targets: vi.fn(), instances: vi.fn(), get: vi.fn(),
}));

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    githubApi: { list: mocks.targets },
    instancesApi: { list: mocks.instances },
    customFieldsApi: { get: mocks.get },
  };
});

beforeEach(() => {
  mocks.targets.mockResolvedValue([{ id: "repo", name: "Repo", repo: "owner/repo", branch: "main" }]);
  mocks.instances.mockResolvedValue([]);
  mocks.get.mockResolvedValue({
    repo_target_id: "repo",
    path: "custom-fields/template.yml",
    exists: true,
    sha: "abc123",
    payload: { custom_fields: [{ name: "cust_id", content_types: ["dcim.device"] }] },
    format_supported: false,
    unsupported_reason: "content_types is unsupported; use object_types (NetBox >= 4.6.8).",
  });
});

test("shows an unsupported-format state for a legacy repository template", async () => {
  render(<CustomFieldsPage />);

  expect(await screen.findByRole("heading", { name: "Unsupported custom-fields template" })).toBeTruthy();
  expect(screen.getByText(/content_types is unsupported; use object_types/)).toBeTruthy();
  expect(screen.getByText(/file was not modified/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Add field" })).toBeNull();
});
