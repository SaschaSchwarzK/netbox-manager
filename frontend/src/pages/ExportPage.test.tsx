import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import ExportPage from "./ExportPage";

const mocks = vi.hoisted(() => ({ instances: vi.fn(), tenants: vi.fn(), schema: vi.fn(), list: vi.fn(), create: vi.fn() }));
vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return { ...actual, instancesApi: { list: mocks.instances }, migrationsApi: { tenants: mocks.tenants }, exportsApi: {
    schema: mocks.schema, list: mocks.list, create: mocks.create, get: vi.fn(), cancel: vi.fn(), remove: vi.fn(),
    downloadUrl: (id: string) => `/api/exports/${id}/download`,
  } };
});

beforeEach(() => {
  mocks.instances.mockResolvedValue([{ id: "instance", name: "NetBox" }]);
  mocks.tenants.mockResolvedValue([{ id: 7, name: "Tenant", slug: "tenant" }]);
  mocks.schema.mockResolvedValue({ object_types: [{ key: "device", label: "Devices",
    fixed: [{ key: "name", label: "Name" }], optional: [{ key: "site", label: "Site" }],
    custom_fields: [{ name: "owner", label: "Owner", type: "text", group_name: "", weight: 1 }] }] });
  mocks.list.mockResolvedValue([]);
  mocks.create.mockResolvedValue({});
});

test("renders fields from schema and enables start after required selections", async () => {
  render(<ExportPage />);
  const instance = await screen.findByLabelText("NetBox instance");
  fireEvent.change(instance, { target: { value: "instance" } });
  await screen.findByRole("option", { name: "Tenant" });
  const start = screen.getByRole("button", { name: "Start export" }) as HTMLButtonElement;
  expect(start.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Tenant"), { target: { value: "7" } });
  fireEvent.click(screen.getByLabelText("Devices"));
  await waitFor(() => expect(start.disabled).toBe(false));
  expect(screen.getByText("Owner")).toBeTruthy();
  expect(screen.getByText("Site")).toBeTruthy();
  expect(screen.getByLabelText("Delimiter")).toBeTruthy();
  fireEvent.click(screen.getByLabelText("Excel"));
  expect(screen.queryByLabelText("Delimiter")).toBeNull();
});

test("jobs show expiry and download only for completed jobs", async () => {
  mocks.list.mockResolvedValue([{ id: "done", instance_name: "NetBox", tenant_name: "Tenant", object_types: ["device"], format: "csv", status: "completed", progress: {}, row_counts: { device: 1 }, created_at: "2026-01-01T00:00:00Z", finished_at: "2026-01-01T00:01:00Z", expires_at: "2026-01-08T00:01:00Z" },
    { id: "old", instance_name: "NetBox", tenant_name: "Tenant", object_types: ["device"], format: "csv", status: "expired", progress: {}, row_counts: {}, created_at: "2025-01-01T00:00:00Z", expires_at: "2025-01-08T00:00:00Z" }]);
  render(<ExportPage />);
  fireEvent.click(screen.getByRole("button", { name: "Jobs" }));
  expect(await screen.findByText("Expires at")).toBeTruthy();
  await waitFor(() => expect(screen.getAllByRole("link", { name: "Download" })).toHaveLength(1));
});
