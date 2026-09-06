import { useEffect, useMemo, useState } from "react";
import { ApiError, api, type Artist, type Tag } from "../api";
import { useAuth } from "../auth";
import { navigate } from "../router";
import { Button, ErrorNote, SkeletonRows } from "../components/ui";

/* Taste onboarding, without Spotify.
 *
 * Browse-first, not search-first: an empty box asking someone to recall an
 * artist name from nothing is the worst possible opening screen, so the most
 * played artists are already on display and search narrows them.
 *
 * Genres and artists are separate because they answer different questions --
 * "what do you listen to" and "what are you in the mood for" -- and the engine
 * weights them differently.
 */

const MIN_PICKS = 3;

export function Onboarding() {
  const { user, refreshUser } = useAuth();
  const [artists, setArtists] = useState<Artist[] | null>(null);
  const [tags, setTags] = useState<Tag[] | null>(null);
  const [query, setQuery] = useState("");
  const [chosenArtists, setChosenArtists] = useState<Set<number>>(new Set());
  const [chosenTags, setChosenTags] = useState<Set<number>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void api.tags(24).then(setTags).catch(() => setTags([]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Debounced so typing does not fire a request per keystroke.
    const timer = window.setTimeout(() => {
      api
        .artists(query || undefined, 48)
        .then((result) => {
          if (!cancelled) setArtists(result);
        })
        .catch(() => {
          if (!cancelled) setArtists([]);
        });
    }, query ? 220 : 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  useEffect(() => {
    // Pre-fill from an existing profile so re-running onboarding shows what is
    // already saved instead of an empty slate.
    void api
      .profile()
      .then((profile) => {
        setChosenArtists(new Set(profile.artists.map((artist) => artist.id)));
        setChosenTags(new Set(profile.tags.map((tag) => tag.id)));
      })
      .catch(() => undefined);
  }, []);

  const total = chosenArtists.size + chosenTags.size;
  const readOnly = user?.is_demo ?? false;

  const selectedArtistList = useMemo(
    () => (artists ?? []).filter((artist) => chosenArtists.has(artist.id)),
    [artists, chosenArtists],
  );

  function toggle<T>(set: Set<T>, value: T, update: (next: Set<T>) => void) {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    update(next);
  }

  async function save() {
    setSaving(true);
    setError(null);
    try {
      await api.saveOnboarding([...chosenArtists], [...chosenTags]);
      await refreshUser();
      navigate("/rooms");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not save your picks.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-5 py-10">
      <header className="mb-8">
        <h1 className="text-xl font-semibold tracking-tight">What do you listen to?</h1>
        <p className="mt-2 max-w-xl text-sm leading-relaxed text-ink-400">
          Pick a few artists and genres. No Spotify account, no OAuth — and you can bring a
          real listening history later if you want sharper recommendations.
        </p>
      </header>

      {readOnly && (
        <div className="mb-6 rounded-panel border border-ink-700 bg-ink-850 px-3.5 py-2.5 text-sm text-ink-400">
          The demo account is read-only, so these picks cannot be saved. Create an account to
          build your own profile.
        </div>
      )}

      <section className="mb-9">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-ink-400">
          Genres
        </h2>
        {tags === null ? (
          <div className="h-8 w-full animate-pulse rounded bg-ink-850" />
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {tags.map((tag) => {
              const active = chosenTags.has(tag.id);
              return (
                <button
                  key={tag.id}
                  type="button"
                  aria-pressed={active}
                  onClick={() => toggle(chosenTags, tag.id, setChosenTags)}
                  className={`rounded-full border px-3 py-1.5 text-sm transition-colors ${
                    active
                      ? "border-amber-400 bg-amber-400/10 text-amber-300"
                      : "border-ink-700 text-ink-300 hover:border-ink-600 hover:text-ink-100"
                  }`}
                >
                  {tag.name}
                </button>
              );
            })}
          </div>
        )}
      </section>

      <section className="mb-9">
        <div className="mb-3 flex items-baseline justify-between gap-4">
          <h2 className="text-xs font-medium uppercase tracking-wide text-ink-400">Artists</h2>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search artists"
            aria-label="Search artists"
            className="w-44 rounded-panel border border-ink-700 bg-ink-900 px-2.5 py-1.5 text-sm placeholder:text-ink-600 focus:border-ink-600 sm:w-56"
          />
        </div>

        {artists === null ? (
          <SkeletonRows rows={4} />
        ) : artists.length === 0 ? (
          <p className="py-8 text-center text-sm text-ink-500">
            Nothing matched “{query}”.
          </p>
        ) : (
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            {artists.map((artist) => {
              const active = chosenArtists.has(artist.id);
              return (
                <button
                  key={artist.id}
                  type="button"
                  aria-pressed={active}
                  onClick={() => toggle(chosenArtists, artist.id, setChosenArtists)}
                  className={`truncate rounded-panel border px-3 py-2 text-left text-sm transition-colors ${
                    active
                      ? "border-amber-400 bg-amber-400/10 text-amber-300"
                      : "border-ink-800 text-ink-300 hover:border-ink-700 hover:text-ink-100"
                  }`}
                  title={artist.name}
                >
                  {artist.name}
                </button>
              );
            })}
          </div>
        )}
      </section>

      {error && <div className="mb-4"><ErrorNote message={error} /></div>}

      {/* Sticky so the count and the action stay reachable while scrolling a
          long grid on a phone. */}
      <div className="sticky bottom-0 -mx-5 flex items-center justify-between gap-4 border-t border-ink-800 bg-ink-950/95 px-5 py-3 backdrop-blur">
        <p className="text-sm text-ink-400">
          {total === 0
            ? `Pick at least ${MIN_PICKS}`
            : `${total} selected${total < MIN_PICKS ? ` — ${MIN_PICKS - total} more` : ""}`}
          {selectedArtistList.length > 0 && (
            <span className="ml-2 hidden text-ink-600 sm:inline">
              {selectedArtistList
                .slice(0, 3)
                .map((artist) => artist.name)
                .join(", ")}
              {selectedArtistList.length > 3 && ` +${selectedArtistList.length - 3}`}
            </span>
          )}
        </p>
        <Button
          variant="primary"
          onClick={() => void save()}
          loading={saving}
          disabled={total < MIN_PICKS || readOnly}
        >
          Save and continue
        </Button>
      </div>
    </div>
  );
}
