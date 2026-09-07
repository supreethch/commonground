import type { RoomMode } from "../api";
import { MODE_BLURBS, MODE_LABELS } from "./ui";

/* Switching a room's mode, in the room.
 *
 * Mode was chosen once at creation and never revisited, which hid the only
 * comparison that shows what the modes do: the *same* six people, the same
 * catalogue, re-ranked three ways. Reading three descriptions is a guess;
 * pressing three buttons and watching the playlist change is the product.
 *
 * A segmented control rather than a dropdown, because all three options and
 * their trade-offs should be visible without a click.
 */

const MODES: RoomMode[] = ["consensus", "discovery", "fair_rotation"];

interface Props {
  value: RoomMode;
  onChange: (mode: RoomMode) => void;
  busy?: boolean;
  disabled?: boolean;
}

export function ModeSwitch({ value, onChange, busy = false, disabled = false }: Props) {
  return (
    <div>
      <div
        role="radiogroup"
        aria-label="Ranking mode"
        className="flex rounded-panel border border-ink-800 p-0.5"
      >
        {MODES.map((mode) => {
          const active = mode === value;
          return (
            <button
              key={mode}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={disabled || busy}
              onClick={() => !active && onChange(mode)}
              className={`flex-1 rounded px-2 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed sm:text-[13px] ${
                active
                  ? "bg-amber-400 text-ink-950"
                  : "text-ink-400 hover:bg-ink-850 hover:text-ink-100"
              }`}
            >
              {MODE_LABELS[mode]}
            </button>
          );
        })}
      </div>
      <p className="mt-2 min-h-[2.5rem] text-xs leading-relaxed text-ink-500">
        {busy ? "Re-ranking the room…" : MODE_BLURBS[value]}
      </p>
    </div>
  );
}
