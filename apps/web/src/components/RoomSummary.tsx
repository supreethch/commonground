import type { Playlist, RoomMember } from "../api";
import { Avatar } from "./Avatar";

/* What the playlist did to the room, as one strip.
 *
 * Every track row already shows who *that* track serves. This answers the
 * question the product is actually about: across the whole hour, who came off
 * worst? A per-track view cannot show that -- someone can be mid-table on every
 * single track and still be the person the playlist quietly ignored.
 *
 * The floor is stated as a number and attributed to a person, because "the
 * least-served member scored 0.71" is a claim someone can disagree with, and
 * "we optimise for fairness" is not.
 */

interface Props {
  playlist: Playlist;
  members: RoomMember[];
}

export function RoomSummary({ playlist, members }: Props) {
  if (playlist.tracks.length === 0 || members.length === 0) return null;

  const perMember = members.map((member) => {
    const scores = playlist.tracks.map((track) => track.member_scores[member.user_id] ?? 0);
    const mean = scores.reduce((total, value) => total + value, 0) / scores.length;
    // How many tracks this member is the top scorer on -- the number that
    // exposes one person quietly driving the whole playlist.
    const led = playlist.tracks.filter((track) => {
      const entries = Object.entries(track.member_scores);
      if (entries.length === 0) return false;
      const best = entries.reduce((a, b) => (b[1] > a[1] ? b : a));
      return best[0] === member.user_id;
    }).length;
    return { member, mean, led };
  });

  const worst = perMember.reduce((a, b) => (b.mean < a.mean ? b : a));
  const best = perMember.reduce((a, b) => (b.mean > a.mean ? b : a));
  const spread = best.mean - worst.mean;

  return (
    <div className="rounded-panel border border-ink-800 bg-ink-900 px-4 py-3.5">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-xs font-medium uppercase tracking-wide text-ink-400">
          How this playlist treats the room
        </h2>
        <p className="text-xs text-ink-500">
          <span className="tabular text-ink-300">{worst.mean.toFixed(2)}</span> floor ·{" "}
          <span className="tabular text-ink-300">{spread.toFixed(2)}</span> spread
        </p>
      </div>

      <ul className="space-y-2">
        {perMember.map(({ member, mean, led }) => {
          const isWorst = member.user_id === worst.member.user_id && members.length > 1;
          return (
            <li key={member.user_id} className="flex items-center gap-2.5">
              <Avatar name={member.display_name} size={20} />
              <span className="w-14 shrink-0 truncate text-xs text-ink-300 sm:w-20">
                {member.display_name}
              </span>
              <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-ink-850">
                <span
                  className={`block h-full rounded-full transition-[width] duration-500 ${
                    isWorst ? "bg-amber-400" : "bg-ink-500"
                  }`}
                  style={{ width: `${Math.max(2, mean * 100)}%` }}
                />
              </span>
              <span className="tabular w-9 shrink-0 text-right text-xs text-ink-400">
                {mean.toFixed(2)}
              </span>
              <span
                className="tabular w-10 shrink-0 text-right text-[11px] text-ink-600"
                title={`${led} of ${playlist.tracks.length} tracks were picked with this member as the best match`}
              >
                {led}/{playlist.tracks.length}
              </span>
            </li>
          );
        })}
      </ul>

      {members.length > 1 && (
        <p className="mt-3 border-t border-ink-800 pt-2.5 text-xs leading-relaxed text-ink-500">
          <span className="text-amber-400">{worst.member.display_name}</span> is served least
          across these {playlist.tracks.length} tracks. The ranking optimises that floor
          rather than the average — the right-hand column counts the tracks each member was
          the best match for.
        </p>
      )}
    </div>
  );
}
