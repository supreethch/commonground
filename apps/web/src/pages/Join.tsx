import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { navigate } from "../router";
import { ErrorNote, Spinner } from "../components/ui";

/** Redeem an invite link, then land in the room.
 *
 *  Runs once on mount. Someone arriving from a shared link should not have to
 *  press anything -- they already made the decision by opening it.
 */
export function Join({ token }: { token: string }) {
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void api
      .joinRoom(token)
      .then((room) => navigate(`/rooms/${room.id}`))
      .catch((caught) =>
        setError(
          caught instanceof ApiError ? caught.message : "Could not use this invite link.",
        ),
      );
  }, [token]);

  return (
    <div className="mx-auto max-w-md px-5 py-20 text-center">
      {error ? (
        <>
          <ErrorNote message={error} />
          <a href="#/rooms" className="mt-4 inline-block text-sm text-ink-400 hover:text-amber-400">
            Go to your rooms
          </a>
        </>
      ) : (
        <p className="flex items-center justify-center gap-2 text-sm text-ink-400">
          <Spinner /> Joining the room…
        </p>
      )}
    </div>
  );
}
