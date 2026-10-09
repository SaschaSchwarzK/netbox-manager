import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePolling } from "./usePolling";

type State = { status: "running" | "completed"; count: number };

describe("usePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
  });

  it("backs off unchanged results and stops at a terminal result", async () => {
    const running: State = { status: "running", count: 1 };
    const poll = vi.fn()
      .mockResolvedValueOnce(running)
      .mockResolvedValueOnce({ status: "completed", count: 2 });
    const onValue = vi.fn();
    renderHook(() => usePolling({
      enabled: true, pollKey: "job", initialValue: running, poll, onValue,
      isTerminal: (value) => value.status === "completed",
    }));

    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(poll).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1999));
    expect(poll).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(poll).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(poll).toHaveBeenCalledTimes(2);
  });

  it("pauses while hidden, resumes immediately, and cleans up", async () => {
    const running: State = { status: "running", count: 1 };
    const poll = vi.fn().mockResolvedValue(running);
    const { unmount } = renderHook(() => usePolling({
      enabled: true, pollKey: "job", initialValue: running, poll, onValue: vi.fn(),
      isTerminal: () => false,
    }));
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(poll).not.toHaveBeenCalled();

    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    document.dispatchEvent(new Event("visibilitychange"));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(poll).toHaveBeenCalledOnce();
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(10000));
    expect(poll).toHaveBeenCalledOnce();
  });
});
