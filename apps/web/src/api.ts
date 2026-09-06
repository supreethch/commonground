/* Typed client for the CommonGround API.
 *
 * One place that knows about tokens, refresh and error shapes, so components
 * deal in data and never in fetch plumbing. Errors arrive as ApiError with the
 * server's own message, because "Something went wrong" is not a useful thing to
 * show someone whose invite has expired.
 */

const BASE = import.meta.env.VITE_API_URL ?? "";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface Tokens {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

export interface User {
  id: string;
  email: string;
  display_name: string;
  is_demo: boolean;
  onboarded_at: string | null;
}

export interface Artist {
  id: number;
  mbid: string;
  name: string;
  listen_count: number;
}

export interface Tag {
  id: number;
  name: string;
  is_genre: boolean;
}

export interface Profile {
  artists: Artist[];
  tags: Tag[];
  listen_count: number;
  onboarded_at: string | null;
}

export interface RoomMember {
  user_id: string;
  display_name: string;
  role: string;
  joined_at: string;
  onboarded: boolean;
}

export interface Room {
  id: string;
  name: string;
  mode: RoomMode;
  status: string;
  owner_id: string;
  created_at: string;
  member_count: number;
}

export interface RoomDetail extends Room {
  members: RoomMember[];
}

export type RoomMode = "consensus" | "discovery" | "fair_rotation";

export interface PlaylistTrack {
  id: number;
  position: number;
  recording_id: number;
  title: string;
  artist: string;
  listen_url: string | null;
  group_score: number;
  member_scores: Record<string, number>;
  contributions: Record<string, number>;
  explanation: { sentence: string; clauses: string[]; sources: string[] };
  votes: number;
}

export interface Playlist {
  id: string;
  room_id: string;
  mode: RoomMode;
  engine_version: string;
  seed: number;
  params: Record<string, unknown>;
  generated_at: string;
  duration_ms: number | null;
  member_ids: string[];
  tracks: PlaylistTrack[];
}

export interface Invite {
  token: string;
  room_id: string;
  expires_at: string;
  max_uses: number;
}

const ACCESS_KEY = "cg.access";
const REFRESH_KEY = "cg.refresh";

export const tokenStore = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  set(tokens: Tokens) {
    localStorage.setItem(ACCESS_KEY, tokens.access_token);
    localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors arrive as a list of per-field objects. Showing
    // the first field's message beats showing "[object Object]".
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
    return response.statusText || "Request failed";
  } catch {
    return response.statusText || "Request failed";
  }
}

let refreshing: Promise<boolean> | null = null;

/** Swap the refresh token for a new pair. Collapsed so that several 401s
 *  arriving at once produce one refresh, not one per request. */
async function refreshTokens(): Promise<boolean> {
  if (refreshing) return refreshing;
  const token = tokenStore.refresh();
  if (!token) return false;

  refreshing = (async () => {
    try {
      const response = await fetch(`${BASE}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: token }),
      });
      if (!response.ok) {
        tokenStore.clear();
        return false;
      }
      tokenStore.set(await response.json());
      return true;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  auth?: boolean;
  retry?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = true, retry = true } = options;
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const token = tokenStore.access();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  // Access tokens are short-lived by design, so a 401 mid-session is expected
  // rather than exceptional: refresh once and replay before surfacing anything.
  if (response.status === 401 && auth && retry && (await refreshTokens())) {
    return request<T>(path, { ...options, retry: false });
  }

  if (!response.ok) throw new ApiError(response.status, await readError(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  signup: (email: string, password: string, displayName: string) =>
    request<Tokens>("/api/auth/signup", {
      method: "POST",
      auth: false,
      body: { email, password, display_name: displayName },
    }),

  login: (email: string, password: string) =>
    request<Tokens>("/api/auth/login", {
      method: "POST",
      auth: false,
      body: { email, password },
    }),

  logout: async () => {
    const token = tokenStore.refresh();
    if (token) {
      await request<void>("/api/auth/logout", {
        method: "POST",
        body: { refresh_token: token },
      }).catch(() => undefined);
    }
    tokenStore.clear();
  },

  me: () => request<User>("/api/auth/me"),

  artists: (query?: string, limit = 40) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (query) params.set("q", query);
    return request<Artist[]>(`/api/artists?${params}`, { auth: false });
  },

  tags: (limit = 40) => request<Tag[]>(`/api/tags?limit=${limit}`, { auth: false }),

  profile: () => request<Profile>("/api/profile"),

  saveOnboarding: (artistIds: number[], tagIds: number[]) =>
    request<Profile>("/api/profile/onboarding", {
      method: "PUT",
      body: { artist_ids: artistIds, tag_ids: tagIds },
    }),

  rooms: () => request<Room[]>("/api/rooms"),

  room: (id: string) => request<RoomDetail>(`/api/rooms/${id}`),

  createRoom: (name: string, mode: RoomMode) =>
    request<RoomDetail>("/api/rooms", { method: "POST", body: { name, mode } }),

  deleteRoom: (id: string) => request<void>(`/api/rooms/${id}`, { method: "DELETE" }),

  createInvite: (roomId: string) =>
    request<Invite>(`/api/rooms/${roomId}/invites`, { method: "POST" }),

  joinRoom: (token: string) =>
    request<RoomDetail>(`/api/rooms/join/${token}`, { method: "POST" }),

  playlist: (roomId: string) => request<Playlist>(`/api/rooms/${roomId}/playlist`),

  generate: (roomId: string, k = 20) =>
    request<Playlist>(`/api/rooms/${roomId}/playlist?k=${k}`, { method: "POST" }),

  history: (roomId: string, limit = 10) =>
    request<Playlist[]>(`/api/rooms/${roomId}/history?limit=${limit}`),

  vote: (roomId: string, trackId: number, value: -1 | 0 | 1) =>
    request<{ track_id: number; value: number; total: number }>(
      `/api/rooms/${roomId}/tracks/${trackId}/vote`,
      { method: "POST", body: { value } },
    ),
};

export interface RoomEvent {
  type: "connected" | "member_joined" | "playlist_generated" | "vote";
  room_id: string;
  [key: string]: unknown;
}

/** Open the room socket.
 *
 * The token goes in the query string because a browser cannot set headers on a
 * WebSocket handshake -- which is why the server accepts only the short-lived
 * access token there, and why this socket is read-only.
 */
export function openRoomSocket(roomId: string, onEvent: (event: RoomEvent) => void): () => void {
  const token = tokenStore.access();
  if (!token) return () => undefined;

  const origin = BASE || window.location.origin;
  const url = new URL(`/api/rooms/${roomId}/ws`, origin);
  url.protocol = url.protocol.replace("http", "ws");
  url.searchParams.set("token", token);

  let socket: WebSocket | null = null;
  let closed = false;
  let attempt = 0;
  let timer: number | undefined;

  const connect = () => {
    if (closed) return;
    socket = new WebSocket(url);
    socket.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as RoomEvent);
      } catch {
        // A frame we cannot parse is not worth tearing the connection down for.
      }
    };
    socket.onopen = () => {
      attempt = 0;
    };
    socket.onclose = () => {
      if (closed) return;
      // Backoff capped at 10s. On the free tier the first connection of the day
      // can land while the service is still waking, so giving up after one
      // failure would leave the room permanently static.
      attempt += 1;
      const delay = Math.min(1000 * 2 ** (attempt - 1), 10_000);
      timer = window.setTimeout(connect, delay);
    };
  };

  connect();

  return () => {
    closed = true;
    if (timer) window.clearTimeout(timer);
    socket?.close();
  };
}
