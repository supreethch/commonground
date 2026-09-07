import { useEffect, useState } from "react";
import type { ReactNode } from "react";

/* Saying out loud why a request is taking so long.
 *
 * Two waits on this demo are long enough to look like a hang, and neither is a
 * bug a spinner can explain:
 *
 *   - Starting the API. It runs on a free tier that suspends the service when
 *     it goes idle, so one visitor pays to start it back up.
 *   - Fitting the model. The recommender is fitted in-process and cached, so
 *     the first playlist after a restart pays for it -- measured at 21.8s on
 *     the deployed instance -- and the single free instance is blocked while
 *     it happens.
 *
 * Deliberately silent below the threshold: a warm sign-in already takes a
 * couple of seconds because Argon2 is meant to be slow, and announcing a delay
 * on every normal login would be a lie that trained people to ignore it.
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

function Notice({ seconds, className, children }: {
  seconds: number | null;
  className: string;
  children: ReactNode;
}) {
  if (seconds === null) return null;
  return (
    <p
      role="status"
      className={`rounded-panel border border-ink-800 bg-ink-900 px-3 py-2.5 text-xs leading-relaxed text-ink-400 ${className}`}
    >
      {children} <span className="tabular text-ink-500">{seconds}s</span>
    </p>
  );
}

/** The API is being started. */
export function WakeNotice({ seconds, className = "" }: { seconds: number | null; className?: string }) {
  return (
    <Notice seconds={seconds} className={className}>
      <span className="text-ink-200">Starting the server.</span> The free tier suspends it
      when it goes idle, so the first request has to boot the API. Everything is quick
      once it is up.
    </Notice>
  );
}

/** The recommender is being fitted -- once per server start, not per playlist. */
export function FitNotice({ seconds, className = "" }: { seconds: number | null; className?: string }) {
  return (
    <Notice seconds={seconds} className={className}>
      <span className="text-ink-200">Fitting the recommendation model.</span> It learns from
      the whole listening history once per server start and is then kept in memory, so this
      wait happens to one visitor and every playlist after it builds in milliseconds.
    </Notice>
  );
}
