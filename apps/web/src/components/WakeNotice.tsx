import { useEffect, useState } from "react";

/* Saying out loud why the first request is slow.
 *
 * The API runs on a free tier that suspends the service after 15 minutes
 * without traffic, so the first visitor of the hour pays for starting it. That
 * is a hosting cost, not a bug, but a spinner cannot say so -- and a spinner
 * that sits there for most of a minute reads as broken, which is the worst
 * possible first impression for a demo someone was linked to.
 *
 * Deliberately silent below the threshold: a warm sign-in already takes a
 * couple of seconds because Argon2 is meant to be slow, and announcing a cold
 * start on every normal login would be a lie that trained people to ignore it.
 */

const THRESHOLD_MS = 3000;

/** Seconds elapsed while `active`, or null until it has been slow long enough
 *  to be worth mentioning. */
export function useElapsedWhile(active: boolean, thresholdMs = THRESHOLD_MS): number | null {
  const [elapsed, setElapsed] = useState<number | null>(null);

  useEffect(() => {
    if (!active) {
      setElapsed(null);
      return;
    }
    const started = Date.now();
    const tick = () => {
      const ms = Date.now() - started;
      setElapsed(ms >= thresholdMs ? Math.round(ms / 1000) : null);
    };
    const timer = window.setInterval(tick, 500);
    return () => window.clearInterval(timer);
  }, [active, thresholdMs]);

  return elapsed;
}

export function WakeNotice({ seconds, className = "" }: { seconds: number | null; className?: string }) {
  if (seconds === null) return null;
  return (
    <p
      role="status"
      className={`rounded-panel border border-ink-800 bg-ink-900 px-3 py-2.5 text-xs leading-relaxed text-ink-400 ${className}`}
    >
      <span className="text-ink-200">Starting the server.</span> The free tier suspends it
      after 15 minutes idle, so the first request has to boot the API and load the model.
      Everything is quick once it is up.{" "}
      <span className="tabular text-ink-500">{seconds}s</span>
    </p>
  );
}
