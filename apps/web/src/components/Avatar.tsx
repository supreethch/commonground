/* A member's initial, in a colour derived from their name.
 *
 * Not decoration: a room of six with plain text labels is a list, and a list is
 * hard to scan. A stable colour per person makes the satisfaction strip, the
 * member row and the summary refer to visibly the same someone.
 *
 * Hue comes from a hash of the name, so it is the same on every device and
 * every render without storing anything. Saturation and lightness are fixed to
 * values that stay legible on the dark ground -- letting the hash choose those
 * too is how palettes end up with unreadable colours.
 */

function hue(name: string): number {
  let hash = 0;
  for (let index = 0; index < name.length; index += 1) {
    hash = (hash * 31 + name.charCodeAt(index)) % 360;
  }
  return hash;
}

export function Avatar({ name, size = 24 }: { name: string; size?: number }) {
  const h = hue(name);
  const letter = name.trim().charAt(0).toUpperCase() || "?";
  return (
    <span
      aria-hidden="true"
      className="inline-flex shrink-0 items-center justify-center rounded-full font-medium"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.46),
        background: `oklch(0.42 0.09 ${h})`,
        color: `oklch(0.93 0.04 ${h})`,
      }}
    >
      {letter}
    </span>
  );
}
