import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";

/* Small shared primitives.
 *
 * Not a component library -- just the handful of things used often enough that
 * spelling them out each time would let the spacing and weight drift apart from
 * screen to screen, which is what makes an interface feel assembled rather than
 * designed.
 */

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  // Exactly one filled amber button per screen: the thing you came to do.
  primary: "bg-amber-400 text-ink-950 hover:bg-amber-300 disabled:bg-ink-700 disabled:text-ink-500",
  secondary: "border border-ink-700 text-ink-100 hover:border-ink-600 hover:bg-ink-850",
  ghost: "text-ink-300 hover:text-ink-100 hover:bg-ink-850",
  danger: "border border-ink-700 text-down-400 hover:border-down-400/50 hover:bg-down-400/10",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  loading?: boolean;
}

export function Button({
  variant = "secondary",
  loading = false,
  disabled,
  children,
  className = "",
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={`inline-flex items-center justify-center gap-2 rounded-panel px-3.5 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed ${VARIANTS[variant]} ${className}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`h-3.5 w-3.5 animate-spin ${className}`}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeOpacity="0.25" strokeWidth="2" />
      <path d="M14.5 8A6.5 6.5 0 0 0 8 1.5" stroke="currentColor" strokeWidth="2" />
    </svg>
  );
}

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  hint?: string;
}

export function Field({ label, hint, id, ...rest }: FieldProps) {
  const inputId = id ?? `field-${label.toLowerCase().replace(/\s+/g, "-")}`;
  return (
    <div>
      <label htmlFor={inputId} className="mb-1.5 block text-xs font-medium text-ink-300">
        {label}
      </label>
      <input
        {...rest}
        id={inputId}
        className="w-full rounded-panel border border-ink-700 bg-ink-900 px-3 py-2 text-sm text-ink-100 placeholder:text-ink-500 focus:border-ink-600"
      />
      {hint && <p className="mt-1.5 text-xs text-ink-500">{hint}</p>}
    </div>
  );
}

export function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-panel border border-ink-800 bg-ink-900 ${className}`}>
      {children}
    </section>
  );
}

/** An error the user can act on. Shows the server's own wording -- an expired
 *  invite and a full room need different responses from the reader. */
export function ErrorNote({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-panel border border-down-400/30 bg-down-400/10 px-3 py-2 text-sm text-down-400"
    >
      {message}
    </p>
  );
}

export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="px-6 py-14 text-center">
      <p className="text-sm font-medium text-ink-300">{title}</p>
      {children && <div className="mx-auto mt-2 max-w-sm text-sm text-ink-500">{children}</div>}
    </div>
  );
}

/** Skeleton rows. A spinner tells you to wait; a skeleton tells you what is
 *  coming and stops the layout jumping when it arrives. */
export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="divide-y divide-ink-800" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="flex items-center gap-3 px-4 py-3.5">
          <div className="h-3 w-4 rounded bg-ink-800" />
          <div className="flex-1 space-y-2">
            <div
              className="h-3 rounded bg-ink-800"
              style={{ width: `${45 + ((index * 13) % 35)}%` }}
            />
            <div
              className="h-2.5 rounded bg-ink-850"
              style={{ width: `${60 + ((index * 7) % 25)}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

export function ModeBadge({ mode }: { mode: string }) {
  const label = MODE_LABELS[mode] ?? mode;
  return (
    <span className="rounded border border-ink-700 px-1.5 py-0.5 text-[11px] uppercase tracking-wide text-ink-400">
      {label}
    </span>
  );
}

export const MODE_LABELS: Record<string, string> = {
  consensus: "Consensus",
  discovery: "Discovery",
  fair_rotation: "Fair rotation",
};

export const MODE_BLURBS: Record<string, string> = {
  consensus: "Protects whoever is worst served. Strict about strong objections.",
  discovery: "Leans towards music none of you have heard, at some cost to safety.",
  fair_rotation: "Takes turns — each slot picks for whoever the room has served least.",
};
