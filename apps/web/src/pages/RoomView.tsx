import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  api,
  openRoomSocket,
  type Playlist,
  type RoomDetail,
  type RoomEvent,
  type RoomMode,
} from "../api";
import { useAuth } from "../auth";
import { Avatar } from "../components/Avatar";
import { ModeSwitch } from "../components/ModeSwitch";
import { RoomSummary } from "../components/RoomSummary";
import { TrackRow } from "../components/TrackRow";
import { Button, Empty, ErrorNote, Panel, SkeletonRows } from "../components/ui";
import { FitNotice, useElapsedWhile } from "../components/WakeNotice";

/* The room: members, the invite link, the playlist, and live voting.
 *
 * Everything that changes state goes over REST; the socket only says that
 * something changed. So a dropped connection degrades to a stale view that
 * repairs itself on the next action, never to a lost vote.
 */

export function RoomView({ roomId }: { roomId: string }) {
  const { user } = useAuth();
  const [room, setRoom] = useState<RoomDetail | null>(null);
  const [playlist, setPlaylist] = useState<Playlist | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const fitting = useElapsedWhile(generating);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const [myVotes, setMyVotes] = useState<Record<number, -1 | 0 | 1>>({});
  const [invite, setInvite] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [switching, setSwitching] = useState(false);

  const reloadPlaylist = useCallback(async () => {
    try {
      const latest = await api.playlist(roomId);
      // A playlist whose tracks are gone -- the catalogue was rebuilt beneath
      // it -- is not a playlist. Showing it as an empty list reads as a
      // failure; treating it as "nothing yet" is both truer and actionable.
      setPlaylist(latest.tracks.length > 0 ? latest : null);
    } catch (caught) {
      // 404 simply means nothing has been generated yet, which is a normal
      // state for a new room rather than an error to shout about.
      if (!(caught instanceof ApiError && caught.status === 404)) throw caught;
      setPlaylist(null);
    }
  }, [roomId]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const detail = await api.room(roomId);
        if (cancelled) return;
        setRoom(detail);
        await reloadPlaylist();
      } catch (caught) {
        if (!cancelled) {
          setError(
            caught instanceof ApiError ? caught.message : "Could not load this room.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [roomId, reloadPlaylist]);

  const playlistRef = useRef<Playlist | null>(null);
  playlistRef.current = playlist;

  useEffect(() => {
    const close = openRoomSocket(roomId, (event: RoomEvent) => {
      if (event.type === "connected") {
        setLive(true);
        return;
      }
      if (event.type === "member_joined") {
        void api.room(roomId).then(setRoom).catch(() => undefined);
        return;
      }
      if (event.type === "mode_changed") {
        void api.room(roomId).then(setRoom).catch(() => undefined);
        return;
      }
      if (event.type === "playlist_generated") {
        void reloadPlaylist().catch(() => undefined);
        return;
      }
      if (event.type === "vote") {
        const trackId = Number(event.track_id);
        const total = Number(event.total);
        const current = playlistRef.current;
        if (!current) return;
        // Patch the one track rather than refetching: a room where four people
        // are voting would otherwise refetch the whole playlist per keystroke.
        setPlaylist({
          ...current,
          tracks: current.tracks.map((track) =>
            track.id === trackId ? { ...track, votes: total } : track,
          ),
        });
      }
    });
    return () => {
      close();
      setLive(false);
    };
  }, [roomId, reloadPlaylist]);

  async function generate() {
    setGenerating(true);
    setError(null);
    try {
      setPlaylist(await api.generate(roomId, 20));
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not build a playlist.",
      );
    } finally {
      setGenerating(false);
    }
  }

  async function changeMode(mode: RoomMode) {
    setSwitching(true);
    setError(null);
    try {
      setRoom(await api.setMode(roomId, mode));
      // Re-rank straight away. Switching the mode and then having to press
      // another button to see what it did would bury the comparison that makes
      // the modes legible.
      setPlaylist(await api.generate(roomId, 20));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not change the mode.");
    } finally {
      setSwitching(false);
    }
  }

  async function vote(trackId: number, value: -1 | 0 | 1) {
    const previous = myVotes[trackId] ?? 0;
    // Optimistic: the button should respond to the tap, not to the round trip.
    setMyVotes((votes) => ({ ...votes, [trackId]: value }));
    try {
      const result = await api.vote(roomId, trackId, value);
      setPlaylist((current) =>
        current
          ? {
              ...current,
              tracks: current.tracks.map((track) =>
                track.id === trackId ? { ...track, votes: result.total } : track,
              ),
            }
          : current,
      );
    } catch {
      setMyVotes((votes) => ({ ...votes, [trackId]: previous }));
      setError("Your vote did not save. Check your connection and try again.");
    }
  }

  async function makeInvite() {
    try {
      const created = await api.createInvite(roomId);
      const link = `${window.location.origin}${window.location.pathname}#/join/${created.token}`;
      setInvite(link);
      try {
        await navigator.clipboard.writeText(link);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 2000);
      } catch {
        // Clipboard permission can be refused; the link is on screen regardless.
      }
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not create an invite link.",
      );
    }
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-3xl px-5 py-10">
        <div className="mb-6 h-7 w-48 animate-pulse rounded bg-ink-850" />
        <Panel>
          <SkeletonRows rows={5} />
        </Panel>
      </div>
    );
  }

  if (!room) {
    return (
      <div className="mx-auto max-w-3xl px-5 py-10">
        <ErrorNote message={error ?? "This room does not exist, or you are not in it."} />
        <a href="#/rooms" className="mt-4 inline-block text-sm text-ink-400 hover:text-amber-400">
          ← Back to rooms
        </a>
      </div>
    );
  }

  const relaxed = playlist?.params?.veto_relaxed === true;
  // Only inviting is withheld from the demo account. Generating and voting are
  // what a visitor came to try, and neither rewrites anyone's taste.
  const canInvite = !(user?.is_demo ?? false);
  const notOnboarded = room.members.filter((member) => !member.onboarded);

  return (
    <div className="mx-auto max-w-3xl px-5 py-8">
      <a href="#/rooms" className="text-sm text-ink-500 hover:text-ink-300">
        ← Rooms
      </a>

      <header className="mt-3 mb-5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">{room.name}</h1>
          <span className="flex items-center gap-1.5 text-xs text-ink-500">
            <span
              className={`h-1.5 w-1.5 rounded-full ${live ? "bg-up-400" : "bg-ink-600"}`}
              aria-hidden="true"
            />
            {live ? "Live" : "Reconnecting"}
          </span>
        </div>
      </header>

      <div className="mb-5 grid gap-4 sm:grid-cols-2">
        <Panel className="p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-xs font-medium uppercase tracking-wide text-ink-400">
              {room.members.length} in the room
            </h2>
            <Button variant="ghost" onClick={() => void makeInvite()} disabled={!canInvite}>
              Invite
            </Button>
          </div>
          <ul className="flex flex-wrap gap-1.5">
            {room.members.map((member) => (
              <li
                key={member.user_id}
                title={
                  member.onboarded
                    ? member.display_name
                    : `${member.display_name} — no taste profile yet`
                }
                className={`flex items-center gap-1.5 rounded-full border py-1 pl-1 pr-2.5 text-xs ${
                  member.onboarded
                    ? "border-ink-700 text-ink-300"
                    : "border-dashed border-ink-700 text-ink-500"
                }`}
              >
                <Avatar name={member.display_name} size={18} />
                {member.display_name}
              </li>
            ))}
          </ul>

          {notOnboarded.length > 0 && (
            <p className="mt-3 border-t border-ink-800 pt-2.5 text-xs leading-relaxed text-ink-500">
              {notOnboarded.map((m) => m.display_name).join(", ")}{" "}
              {notOnboarded.length === 1 ? "has" : "have"} no taste profile yet, so their
              scores come from group priors rather than a prediction.
            </p>
          )}

          {invite && (
            <div className="mt-3 border-t border-ink-800 pt-3">
              <p className="mb-1.5 text-xs text-ink-400">
                {copied ? "Link copied — share it with the room." : "Share this link:"}
              </p>
              <code className="block overflow-x-auto rounded bg-ink-950 px-2.5 py-2 text-xs text-ink-300">
                {invite}
              </code>
            </div>
          )}
        </Panel>

        <Panel className="p-4">
          <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-ink-400">
            Ranking mode
          </h2>
          <ModeSwitch
            value={room.mode}
            onChange={(mode) => void changeMode(mode)}
            busy={switching}
            disabled={generating}
          />
        </Panel>
      </div>

      {playlist && playlist.tracks.length > 0 && (
        <div className="mb-5">
          <RoomSummary playlist={playlist} members={room.members} />
        </div>
      )}

      {error && <div className="mb-4"><ErrorNote message={error} /></div>}

      <div className="mb-3 flex items-center justify-between gap-4">
        <h2 className="text-xs font-medium uppercase tracking-wide text-ink-400">Playlist</h2>
        <div className="flex items-center gap-2">
          {playlist?.duration_ms != null && (
            <span className="tabular text-xs text-ink-600">
              built in {playlist.duration_ms}ms
            </span>
          )}
          <Button
            variant={playlist ? "secondary" : "primary"}
            onClick={() => void generate()}
            loading={generating}
          >
            {playlist ? "Regenerate" : "Build playlist"}
          </Button>
        </div>
      </div>

      {relaxed && (
        <p className="mb-3 rounded-panel border border-amber-400/30 bg-amber-400/[0.07] px-3.5 py-2.5 text-xs leading-relaxed text-amber-300">
          This room was too divided to hold the strict floor, so the veto was loosened to fill
          twenty tracks. Some members may find a few of these harder going.
        </p>
      )}

      <Panel>
        <FitNotice seconds={fitting} className="mb-4" />
        {generating && !playlist ? (
          <SkeletonRows rows={6} />
        ) : !playlist ? (
          <Empty title="No playlist yet">
            Build one and every track will come with a sentence saying why it is there — and a
            strip showing who it works for.
          </Empty>
        ) : (
          <div className="divide-y divide-ink-800">
            {playlist.tracks.map((track) => (
              <TrackRow
                key={track.id}
                track={track}
                members={room.members}
                myVote={myVotes[track.id] ?? 0}
                onVote={(value) => void vote(track.id, value)}
              />
            ))}
          </div>
        )}
      </Panel>

      {playlist && (
        <p className="mt-3 text-xs leading-relaxed text-ink-600">
          Ranked by engine {playlist.engine_version}, seed {playlist.seed}. The same room, mode
          and seed always produce the same playlist — which is what makes the reasons above
          checkable.
        </p>
      )}
    </div>
  );
}
