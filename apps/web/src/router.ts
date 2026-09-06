import { useEffect, useState } from "react";

/* Hash routing.
 *
 * Deliberately not a router library. There are five screens, and hash routes
 * mean the built frontend is a static bundle that needs no server rewrite rule
 * -- which is what lets it sit on a free static host next to an API on another
 * origin. Same choice pulse made.
 */

export function useHashRoute(): [string, (path: string) => void] {
  const [route, setRoute] = useState(() => window.location.hash.slice(1) || "/");

  useEffect(() => {
    const onChange = () => setRoute(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return [route, (path: string) => { window.location.hash = path; }];
}

export function navigate(path: string): void {
  window.location.hash = path;
}
