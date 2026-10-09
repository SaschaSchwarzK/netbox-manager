import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import ReferenceDataPage from "./ReferenceDataPage";

const mocks = vi.hoisted(() => ({
  list: vi.fn(), instances: vi.fn(), schema: vi.fn(), get: vi.fn(), save: vi.fn(), importScan: vi.fn(),
}));
vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, githubApi: { list: mocks.list }, instancesApi: { list: mocks.instances }, referenceDataApi: { schema: mocks.schema, get: mocks.get, save: mocks.save, importScan: mocks.importScan } };
});

beforeEach(() => {
  mocks.list.mockResolvedValue([{ id: "repo", name: "Repo" }]);
  mocks.instances.mockResolvedValue([{ id: "instance", name: "NetBox" }]);
  mocks.schema.mockResolvedValue({ push_order: ["tags"], kinds: { tags: { label: "Tags", fields: ["name", "color"], json_schema: {}, file: "tags.yml" } } });
  mocks.get.mockResolvedValue({ exists: true, sha: "sha", payload: { items: [{ name: "production", color: "ff0000" }] } });
  mocks.importScan.mockResolvedValue({
    payload: { items: [{ name: "imported", color: "00ff00" }] },
    diff: { missing_on_instance: [], extra_on_instance: ["imported"], changed: [] },
  });
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

test("imports selected NetBox reference data into the draft", async () => {
  render(<ReferenceDataPage />);
  await screen.findByDisplayValue("ff0000");
  const source = await screen.findByLabelText("Source instance");
  fireEvent.change(source, { target: { value: "instance" } });
  fireEvent.click(screen.getByRole("button", { name: "Scan for import" }));
  fireEvent.click(await screen.findByLabelText(/imported/));
  fireEvent.click(await screen.findByRole("button", { name: "Import selected into draft" }));

  expect(await screen.findByDisplayValue("imported")).toBeTruthy();
  expect(screen.getByText(/Added 1 imported item/)).toBeTruthy();
});
