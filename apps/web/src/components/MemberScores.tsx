import { useId, useState } from "react";

/* The per-member satisfaction strip.
 *
 * This is the interface element the whole product exists for. Every other group
 * playlist app shows you a list of songs; the claim here is that you can see
 * *who each track works for* and who is being asked to put up with it -- so that
 * has to be visible on every row, not buried in a detail view.
 *
 * One bar per member, height proportional to their predicted satisfaction. The
 * least-satisfied member is marked in amber, because "who is worst served" is
 * the number the ranking is built around and an average would hide it.
 *
 * Deliberately small and readable at a glance: it sits inside a track row and
 * has to survive a 360px viewport without becoming a chart.
 */

export interface MemberScore {
  id: string;
  name: string;
  score: number;
}

interface Props {
  members: MemberScore[];
  /** Compact hides labels; the room view uses it, the detail panel does not. */
  compact?: boolean;
}

/** A short label for the panel's left column.
 *
 * First names are usually short enough to show in full, and "Alex" reads far
 * better than "AL" -- abbreviating a four-letter name to two letters makes the
 * panel look like a spreadsheet. Only genuinely long names are shortened.
 */
function shortLabel(name: string): string {
  const trimmed = name.trim();
  if (!trimmed) return "?";
  const first = trimmed.split(/\s+/)[0] ?? trimmed;
  if (first.length <= 9) return first;
  return `${first.slice(0, 8)}\u2026`;
}

export function MemberScores({ members, compact = false }: Props) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  if (members.length === 0) return null;

  const lowest = members.reduce((worst, member) =>
    member.score < worst.score ? member : worst,
  );

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls={panelId}
        className="flex items-end gap-[3px] rounded px-1 py-1 transition-colors hover:bg-ink-800"
        title="Predicted satisfaction per member"
      >
        {members.map((member) => {
          const isLowest = member.id === lowest.id && members.length > 1;
          // A floor of 3px keeps a member at zero visible. A bar that vanishes
          // reads as "no data", which is a different fact from "served badly".
          const height = Math.max(3, Math.round(member.score * 22));
          return (
            <span
              key={member.id}
              className={`w-[5px] rounded-[1px] ${isLowest ? "bg-amber-400" : "bg-ink-500"}`}
              style={{ height }}
            />
          );
        })}
      </button>

      {open && (
        <div
          id={panelId}
          className="absolute right-0 z-20 mt-1 w-52 rounded-panel border border-ink-700 bg-ink-850 p-2.5 shadow-xl shadow-black/40 rise"
        >
          <p className="mb-2 text-[11px] uppercase tracking-wide text-ink-400">
            Predicted satisfaction
          </p>
          <ul className="space-y-1.5">
            {members.map((member) => {
              const isLowest = member.id === lowest.id && members.length > 1;
              return (
                <li key={member.id} className="flex items-center gap-2">
                  <span className="w-16 shrink-0 truncate text-xs text-ink-300">
                    {compact ? shortLabel(member.name) : member.name}
                  </span>
                  <span className="h-1 flex-1 overflow-hidden rounded-full bg-ink-800">
                    <span
                      className={`block h-full ${isLowest ? "bg-amber-400" : "bg-ink-500"}`}
                      style={{ width: `${Math.max(2, member.score * 100)}%` }}
                    />
                  </span>
                  <span className="tabular w-8 shrink-0 text-right text-xs text-ink-400">
                    {member.score.toFixed(2)}
                  </span>
                </li>
              );
            })}
          </ul>
          {members.length > 1 && (
            <p className="mt-2 border-t border-ink-700 pt-2 text-[11px] leading-snug text-ink-400">
              <span className="text-amber-400">{lowest.name}</span> is served least by this
              track.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
