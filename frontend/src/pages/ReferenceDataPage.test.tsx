import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import ReferenceDataPage from "./ReferenceDataPage";

const mocks = vi.hoisted(() => ({
  list: vi.fn(), instances: vi.fn(), schema: vi.fn(), get: vi.fn(), save: vi.fn(),
}));
vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, githubApi: { list: mocks.list }, instancesApi: { list: mocks.instances }, referenceDataApi: { schema: mocks.schema, get: mocks.get, save: mocks.save } };
});

beforeEach(() => {
  mocks.list.mockResolvedValue([{ id: "repo", name: "Repo" }]);
  mocks.instances.mockResolvedValue([]);
  mocks.schema.mockResolvedValue({ push_order: ["tags"], kinds: { tags: { label: "Tags", fields: ["name", "color"], json_schema: {}, file: "tags.yml" } } });
  mocks.get.mockResolvedValue({ exists: true, sha: "sha", payload: { items: [{ name: "production", color: "ff0000" }] } });
});

test("renders registry tabs and schema fields", async () => {
  render(<ReferenceDataPage />);
  expect(await screen.findByRole("tab", { name: "Tags" })).toBeTruthy();
  expect(await screen.findByDisplayValue("ff0000")).toBeTruthy();
});

test("shows API errors", async () => {
  mocks.get.mockRejectedValueOnce(new Error("Could not load reference data"));
  render(<ReferenceDataPage />);
  await waitFor(() => expect(screen.getByText("Could not load reference data")).toBeTruthy());
});
