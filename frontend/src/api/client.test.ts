import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, instancesApi } from "./client";

function response(body: string, status: number) {
  return new Response(body, { status, headers: { "Content-Type": "application/json" } });
}

describe("API client errors and GET de-duplication", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("preserves status and parsed detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response('{"detail":"bad input"}', 422)));
    await expect(instancesApi.list()).rejects.toMatchObject({ status: 422, detail: "bad input" });
  });

  it("dispatches the unauthorized event for a 401", async () => {
    const listener = vi.fn();
    window.addEventListener("nbm:unauthorized", listener);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response('{"detail":"expired"}', 401)));
    await expect(instancesApi.list()).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledOnce();
    window.removeEventListener("nbm:unauthorized", listener);
  });

  it("uses a friendly 403 message and tolerates non-JSON bodies", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response("forbidden", 403)));
    await expect(instancesApi.list()).rejects.toThrow("You don't have permission to perform this action.");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response("upstream exploded", 502)));
    await expect(instancesApi.list()).rejects.toThrow("Request failed (502).");
  });

  it("shares one in-flight request between identical GETs", async () => {
    let resolveFetch!: (value: Response) => void;
    const fetchMock = vi.fn().mockReturnValue(new Promise<Response>((resolve) => { resolveFetch = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const first = instancesApi.list();
    const second = instancesApi.list();
    expect(fetchMock).toHaveBeenCalledOnce();
    resolveFetch(response("[]", 200));
    await expect(Promise.all([first, second])).resolves.toEqual([[], []]);
  });
});
