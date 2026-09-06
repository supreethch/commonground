import { useEffect, useRef, useState } from "react";
import type { PlaylistTrack, RoomMember } from "../api";
import { MemberScores } from "./MemberScores";

/* One track in a room's playlist.
 *
 * The layout is a deliberate hierarchy rather than a card: position, then title,
 * then the reason, then the satisfaction strip and the vote control. The reason
 * is set in a serif and given room to breathe because it is the thing this
 * product is actually for -- shrinking it to a caption would be admitting it is
 * decoration.
 */

interface Props {
  track: PlaylistTrack;
  members: RoomMember[];
  myVote: -1 | 0 | 1;
  onVote: (value: -1 | 0 | 1) => void;
  disabled?: boolean;
}

export function TrackRow({ track, members, myVote, onVote, disabled = false }: Props) {
  const [flash, setFlash] = useState(false);
  const previousVotes = useRef(track.votes);

  useEffect(() => {
    if (previousVotes.current !== track.votes) {
      previousVotes.current = track.votes;
      setFlash(true);
      const timer = window.setTimeout(() => setFlash(false), 1100);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [track.votes]);

  const scores = members.map((member) => ({
    id: member.user_id,
    name: member.display_name,
    score: track.member_scores[member.user_id] ?? 0,
  }));

  return (
    <article className={`px-4 py-3.5 sm:px-5 ${flash ? "just-changed" : ""}`}>
      <div className="flex items-start gap-3 sm:gap-4">
        <span className="tabular mt-0.5 w-5 shrink-0 text-right text-sm text-ink-500">
          {track.position + 1}
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <h3 className="truncate text-[15px] font-medium text-ink-100">{track.title}</h3>
          </div>
          <p className="truncate text-sm text-ink-400">{track.artist}</p>

          <p className="reason mt-2">{track.explanation.sentence}</p>

          <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {track.listen_url && (
              <a
                href={track.listen_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 text-xs text-ink-400 underline-offset-2 hover:text-amber-400 hover:underline"
              >
                <PlayIcon />
                Listen
              </a>
            )}
            {/* No audio is hosted, so this is a link out and says so rather
                than pretending to be a player. */}
          </div>
        </div>

        <div className="flex shrink-0 flex-col items-end gap-2">
          <MemberScores members={scores} compact />
          <VoteControl
            value={myVote}
            total={track.votes}
            disabled={disabled}
            onVote={onVote}
          />
        </div>
      </div>
    </article>
  );
}

function VoteControl({
  value,
  total,
  disabled,
  onVote,
}: {
  value: -1 | 0 | 1;
  total: number;
  disabled: boolean;
  onVote: (value: -1 | 0 | 1) => void;
}) {
  return (
    <div className="flex items-center gap-0.5 rounded-panel border border-ink-800 p-0.5">
      <button
        type="button"
        disabled={disabled}
        aria-pressed={value === 1}
        aria-label="Vote up"
        // Clicking the vote you already hold retracts it, which is what people
        // expect and saves a separate "undo" affordance.
        onClick={() => onVote(value === 1 ? 0 : 1)}
        className={`rounded p-1 transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
          value === 1 ? "text-up-400" : "text-ink-500 hover:text-ink-300"
        }`}
      >
        <ThumbIcon />
      </button>
      <span
        className={`tabular min-w-[1.5rem] text-center text-xs ${
          total > 0 ? "text-up-400" : total < 0 ? "text-down-400" : "text-ink-500"
        }`}
      >
        {total > 0 ? `+${total}` : total}
      </span>
      <button
        type="button"
        disabled={disabled}
        aria-pressed={value === -1}
        aria-label="Vote down"
        onClick={() => onVote(value === -1 ? 0 : -1)}
        className={`rounded p-1 transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
          value === -1 ? "text-down-400" : "text-ink-500 hover:text-ink-300"
        }`}
      >
        <ThumbIcon down />
      </button>
    </div>
  );
}

function ThumbIcon({ down = false }: { down?: boolean }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={`h-3.5 w-3.5 ${down ? "rotate-180" : ""}`}
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M9.2 1.4a1 1 0 0 1 1.74.67v3.1h2.42a1.6 1.6 0 0 1 1.57 1.93l-.94 4.4A2 2 0 0 1 12.03 13H6.4V6.2l2.8-4.8ZM4.9 6.3H2.6a1 1 0 0 0-1 1v4.9a1 1 0 0 0 1 1h2.3V6.3Z" />
    </svg>
  );
}

function PlayIcon() {
  return (
    <svg viewBox="0 0 12 12" className="h-2.5 w-2.5" fill="currentColor" aria-hidden="true">
      <path d="M2.5 1.3a.6.6 0 0 1 .92-.5l6.6 4.2a.6.6 0 0 1 0 1l-6.6 4.2a.6.6 0 0 1-.92-.5V1.3Z" />
    </svg>
  );
}
