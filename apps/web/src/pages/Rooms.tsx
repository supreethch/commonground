import { useEffect, useState } from "react";
import { ApiError, api, type Room, type RoomMode } from "../api";
import { useAuth } from "../auth";
import { navigate } from "../router";
import {
  Button,
  Empty,
  ErrorNote,
  MODE_BLURBS,
  MODE_LABELS,
  ModeBadge,
  Panel,
  SkeletonRows,
} from "../components/ui";

const MODES: RoomMode[] = ["consensus", "discovery", "fair_rotation"];

export function Rooms() {
  const { user } = useAuth();
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    void api
      .rooms()
      .then(setRooms)
      .catch((caught) => {
        setError(caught instanceof ApiError ? caught.message : "Could not load your rooms.");
        setRooms([]);
      });
  }, []);

  return (
    <div className="mx-auto max-w-3xl px-5 py-10">
      <header className="mb-7 flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Rooms</h1>
          <p className="mt-1 text-sm text-ink-400">
            A room is a group and a mode. Invite people, then generate a playlist.
          </p>
        </div>
        <Button variant="primary" onClick={() => setCreating(true)} disabled={user?.is_demo}>
          New room
        </Button>
      </header>

      {user?.is_demo && (
        <p className="mb-5 rounded-panel border border-ink-700 bg-ink-850 px-3.5 py-2.5 text-sm text-ink-400">
          The demo account is read-only. You can open any room it belongs to, but creating one
          needs your own account.
        </p>
      )}

      {creating && (
        <CreateRoom
          onCancel={() => setCreating(false)}
          onCreated={(room) => navigate(`/rooms/${room.id}`)}
        />
      )}

      {error && <div className="mb-4"><ErrorNote message={error} /></div>}

      <Panel>
        {rooms === null ? (
          <SkeletonRows rows={3} />
        ) : rooms.length === 0 ? (
          <Empty title="No rooms yet">
            Create one, pick a mode, and share the invite link with whoever is in the car.
          </Empty>
        ) : (
          <ul className="divide-y divide-ink-800">
            {rooms.map((room) => (
              <li key={room.id}>
                <a
                  href={`#/rooms/${room.id}`}
                  className="flex items-center gap-4 px-4 py-3.5 transition-colors hover:bg-ink-850"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[15px] font-medium">{room.name}</p>
                    <p className="mt-0.5 text-xs text-ink-500">
                      {room.member_count} {room.member_count === 1 ? "member" : "members"}
                    </p>
                  </div>
                  <ModeBadge mode={room.mode} />
                </a>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}

function CreateRoom({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (room: Room) => void;
}) {
  const [name, setName] = useState("");
  const [mode, setMode] = useState<RoomMode>("consensus");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <Panel className="mb-6 p-4 rise">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError(null);
          try {
            onCreated(await api.createRoom(name, mode));
          } catch (caught) {
            setError(
              caught instanceof ApiError ? caught.message : "Could not create the room.",
            );
          } finally {
            setBusy(false);
          }
        }}
      >
        <label htmlFor="room-name" className="mb-1.5 block text-xs font-medium text-ink-300">
          Room name
        </label>
        <input
          id="room-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="Friday drive"
          required
          maxLength={80}
          autoFocus
          className="mb-5 w-full rounded-panel border border-ink-700 bg-ink-900 px-3 py-2 text-sm placeholder:text-ink-600 focus:border-ink-600"
        />

        <fieldset className="mb-5">
          <legend className="mb-2 text-xs font-medium text-ink-300">Mode</legend>
          {/* Each mode carries its trade-off in the option itself. Picking
              between three words with no explanation is a guess, and the
              difference between these three is the point of the product. */}
          <div className="space-y-1.5">
            {MODES.map((option) => (
              <label
                key={option}
                className={`flex cursor-pointer gap-3 rounded-panel border p-3 transition-colors ${
                  mode === option
                    ? "border-amber-400/60 bg-amber-400/[0.06]"
                    : "border-ink-800 hover:border-ink-700"
                }`}
              >
                <input
                  type="radio"
                  name="mode"
                  value={option}
                  checked={mode === option}
                  onChange={() => setMode(option)}
                  className="mt-1 accent-amber-400"
                />
                <span>
                  <span className="block text-sm font-medium">{MODE_LABELS[option]}</span>
                  <span className="mt-0.5 block text-xs leading-relaxed text-ink-500">
                    {MODE_BLURBS[option]}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        {error && <div className="mb-4"><ErrorNote message={error} /></div>}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!name.trim()}>
            Create room
          </Button>
        </div>
      </form>
    </Panel>
  );
}
