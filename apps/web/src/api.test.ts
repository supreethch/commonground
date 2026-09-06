import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, tokenStore } from "./api";

/* Client tests, weighted toward the token refresh.
 *
 * Access tokens are short-lived by design, so a 401 mid-session is the normal
 * case rather than an error. If the refresh-and-replay is wrong, the app logs
 * people out every half hour and it looks like a backend fault.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api client", () => {
  beforeEach(() => {
    localStorage.clear();
    tokenStore.set({ access_token: "old", refresh_token: "refresh", expires_in: 1800 });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("replays the request once after refreshing an expired access token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "not authenticated" }, 401))
      .mockResolvedValueOnce(
        jsonResponse({ access_token: "new", refresh_token: "r2", expires_in: 1800 }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: "u1", display_name: "Alex" }));
    vi.stubGlobal("fetch", fetchMock);

    const user = await api.me();

    expect(user.display_name).toBe("Alex");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(tokenStore.access()).toBe("new");
  });

  it("gives up rather than looping when the refresh itself fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "not authenticated" }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid" }, 401));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    // Two calls, not an endless retry, and the dead tokens are cleared.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(tokenStore.access()).toBeNull();
  });

  it("surfaces the server's own message rather than a generic one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "this invite is not valid" }, 404)),
    );

    await expect(api.joinRoom("nope")).rejects.toThrowError("this invite is not valid");
  });

  it("reads the first field message out of a validation error", async () => {
    // FastAPI returns a list of per-field objects; showing "[object Object]"
    // to a user is worse than showing nothing.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: [{ loc: ["body", "mode"], msg: "mode must be one of..." }] }, 422),
      ),
    );

    await expect(api.createRoom("R", "consensus")).rejects.toThrowError(/mode must be one of/);
  });

  it("does not attach a token to public catalogue requests", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await api.artists("radio");

    const headers = fetchMock.mock.calls[0]?.[1]?.headers ?? {};
    expect(headers.Authorization).toBeUndefined();
  });

  it("clears both tokens on sign out even if the server call fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    await api.logout();

    expect(tokenStore.access()).toBeNull();
    expect(tokenStore.refresh()).toBeNull();
  });
});
