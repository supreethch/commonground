import { useAuth } from "./auth";
import { useHashRoute } from "./router";
import { Join } from "./pages/Join";
import { Onboarding } from "./pages/Onboarding";
import { RoomView } from "./pages/RoomView";
import { Rooms } from "./pages/Rooms";
import { SignIn } from "./pages/SignIn";
import { Button, Spinner } from "./components/ui";

export function App() {
  const { user, loading, signOut } = useAuth();
  const [route] = useHashRoute();

  if (loading) {
    return (
      <div className="flex min-h-dvh items-center justify-center text-ink-500">
        <Spinner />
      </div>
    );
  }

  // An invite link is the one route that survives being signed out: the token
  // stays in the hash, so signing in lands back here and the join completes.
  const joinMatch = route.match(/^\/join\/(.+)$/);

  if (!user) return <SignIn />;

  const roomMatch = route.match(/^\/rooms\/([0-9a-f-]{36})$/i);

  let page: React.ReactNode;
  if (joinMatch?.[1]) page = <Join token={joinMatch[1]} />;
  else if (roomMatch?.[1]) page = <RoomView roomId={roomMatch[1]} />;
  else if (route.startsWith("/onboarding")) page = <Onboarding />;
  else page = <Rooms />;

  return (
    <div className="min-h-dvh">
      <nav className="border-b border-ink-800">
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-4 px-5 py-3">
          <a href="#/rooms" className="text-sm font-semibold tracking-tight">
            CommonGround
          </a>
          <div className="flex items-center gap-1">
            <a
              href="#/onboarding"
              className="rounded-panel px-2.5 py-1.5 text-sm text-ink-400 transition-colors hover:bg-ink-850 hover:text-ink-100"
            >
              My taste
            </a>
            <Button variant="ghost" onClick={() => void signOut()}>
              Sign out
            </Button>
          </div>
        </div>
      </nav>
      {page}
      <footer className="mx-auto max-w-3xl px-5 pb-10 pt-6 text-xs leading-relaxed text-ink-600">
        Catalogue and listening data from{" "}
        <a href="https://musicbrainz.org" className="hover:text-ink-400" target="_blank" rel="noreferrer">
          MusicBrainz
        </a>{" "}
        and{" "}
        <a href="https://listenbrainz.org" className="hover:text-ink-400" target="_blank" rel="noreferrer">
          ListenBrainz
        </a>
        . Genre tags are CC BY-NC-SA; this is a non-commercial portfolio project. No audio is
        hosted — tracks link out.
      </footer>
    </div>
  );
}
