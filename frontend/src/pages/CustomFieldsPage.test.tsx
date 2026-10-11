import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import CustomFieldsPage from "./CustomFieldsPage";

const mocks = vi.hoisted(() => ({
  targets: vi.fn(), instances: vi.fn(), get: vi.fn(), save: vi.fn(),
}));

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    githubApi: { list: mocks.targets },
    instancesApi: { list: mocks.instances },
    customFieldsApi: { get: mocks.get, save: mocks.save },
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
  mocks.save.mockResolvedValue({ pr_number: 42, pr_url: "https://github.example/pull/42" });
});

test("shows an unsupported-format state for a legacy repository template", async () => {
  render(<CustomFieldsPage />);

  expect(await screen.findByRole("heading", { name: "Unsupported custom-fields template" })).toBeTruthy();
  expect(screen.getByText(/content_types is unsupported; use object_types/)).toBeTruthy();
  expect(screen.getByText(/file was not modified/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "+ Add Custom Field" })).toHaveProperty("disabled", true);
  expect(screen.getByRole("button", { name: "Saved" })).toHaveProperty("disabled", true);
  fireEvent.click(screen.getByRole("button", { name: "Import / Push / Drift" }));
  expect(screen.getByRole("heading", { name: "Restore custom-field values" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Scan for differences" })).toHaveProperty("disabled", true);
});

test("offers to migrate legacy content_types in a pull request", async () => {
  render(<CustomFieldsPage />);

  fireEvent.click(await screen.findByRole("button", { name: "Fix template and open pull request" }));

  await vi.waitFor(() => expect(mocks.save).toHaveBeenCalledOnce());
  const request = mocks.save.mock.calls[0][1];
  expect(request.payload.custom_fields[0].content_types).toBeUndefined();
  expect(request.payload.custom_fields[0].object_types).toEqual(["dcim.device"]);
  expect(await screen.findByText(/Correction proposed in/)).toBeTruthy();
});
