import { useState } from "react";
import { ApiError } from "../api";
import { useAuth } from "../auth";
import { Button, ErrorNote, Field } from "../components/ui";
import { WakeNotice, useElapsedWhile } from "../components/WakeNotice";

/* Sign in / sign up, and the demo shortcut.
 *
 * The demo account is offered first and explained, because the most likely
 * visitor is someone who wants to see the thing work and will not create an
 * account to do it. It is honest about being read-only rather than letting
 * someone discover that by clicking a button that quietly fails.
 */

const DEMO_EMAIL = "alex@commonground.demo";
const DEMO_PASSWORD = "demo-read-only";

export function SignIn() {
  const { signIn, signUp } = useAuth();
  const [mode, setMode] = useState<"in" | "up">("in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState<"form" | "demo" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const waking = useElapsedWhile(busy !== null);

  async function attempt(run: () => Promise<void>, which: "form" | "demo") {
    setBusy(which);
    setError(null);
    try {
      await run();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mx-auto flex min-h-dvh max-w-md flex-col justify-center px-5 py-12">
      <header className="mb-9">
        <h1 className="text-2xl font-semibold tracking-tight">CommonGround</h1>
        <p className="mt-2 text-[15px] leading-relaxed text-ink-400">
          Playlists for a group, ranked on who is served <em className="text-ink-300">least</em>{" "}
          rather than on the average — and every track says why it is there.
        </p>
      </header>

      <div className="mb-7">
        <Button
          variant="primary"
          className="w-full"
          loading={busy === "demo"}
          disabled={busy !== null}
          onClick={() => attempt(() => signIn(DEMO_EMAIL, DEMO_PASSWORD), "demo")}
        >
          Try the demo account
        </Button>
        <p className="mt-2 text-xs leading-relaxed text-ink-500">
          Signs you in as Alex, one of six seeded listeners with real, deliberately
          conflicting taste. Read-only — it can browse rooms but not change a profile.
        </p>
        {busy === "demo" && <WakeNotice seconds={waking} className="mt-3" />}
      </div>

      <div className="mb-7 flex items-center gap-3" aria-hidden="true">
        <span className="h-px flex-1 bg-ink-800" />
        <span className="text-[11px] uppercase tracking-wide text-ink-600">or</span>
        <span className="h-px flex-1 bg-ink-800" />
      </div>

      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void attempt(
            () =>
              mode === "in"
                ? signIn(email, password)
                : signUp(email, password, displayName),
            "form",
          );
        }}
      >
        {mode === "up" && (
          <Field
            label="Name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            autoComplete="name"
            required
            maxLength={60}
          />
        )}
        <Field
          label="Email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          autoComplete="email"
          required
        />
        <Field
          label="Password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete={mode === "in" ? "current-password" : "new-password"}
          required
          minLength={mode === "up" ? 10 : undefined}
          hint={mode === "up" ? "At least 10 characters." : undefined}
        />

        {error && <ErrorNote message={error} />}
        {busy === "form" && <WakeNotice seconds={waking} />}

        <Button
          type="submit"
          variant="secondary"
          className="w-full"
          loading={busy === "form"}
          disabled={busy !== null}
        >
          {mode === "in" ? "Sign in" : "Create account"}
        </Button>
      </form>

      <p className="mt-5 text-center text-sm text-ink-500">
        {mode === "in" ? "No account yet?" : "Already have one?"}{" "}
        <button
          type="button"
          className="text-ink-300 underline underline-offset-2 hover:text-amber-400"
          onClick={() => {
            setMode(mode === "in" ? "up" : "in");
            setError(null);
          }}
        >
          {mode === "in" ? "Create one" : "Sign in"}
        </button>
      </p>
    </div>
  );
}
