"""Match parsed listens onto catalogue recordings.

Two passes, in order of confidence:

  1. **MBID** -- exact, when the export carried one (ListenBrainz usually does).
  2. **Normalised artist + title** -- everything else.

Deliberately no fuzzy matching. Edit distance over track titles produces
confident nonsense ("1979" matching "1999"), and a wrong match is worse than a
miss here: a miss loses one data point, a wrong match teaches the recommender
that a user likes something they have never heard. The match rate is reported to
the user instead of being quietly inflated.

`normalise` is pure and is the part worth testing hard.
"""

from __future__ import annotations

import re
import unicodedata

# Trailing parenthetical and bracketed noise: "(Remastered 2011)",
# "[Explicit]", "- Radio Edit". These differ between Spotify and MusicBrainz for
# the same recording, and leaving them in loses most of the catalogue.
_TRAILING_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"feat\.?|featuring|with|remaster(?:ed)?|re-?master|explicit|clean|"
    r"radio edit|single version|album version|mono|stereo|live|bonus track|"
    r"deluxe|expanded|anniversary|remix"
    r")\b[^\)\]]*[\)\]]\s*",
    re.IGNORECASE,
)

_TRAILING_DASH_NOISE = re.compile(
    r"\s+-\s+(?:"
    r"remaster(?:ed)?(?:\s+\d{4})?|re-?master(?:ed)?|radio edit|single version|"
    r"album version|mono|stereo|\d{4} remaster(?:ed)?"
    r")\s*$",
    re.IGNORECASE,
)

# Apostrophes are deleted rather than turned into a space, so "Don't" becomes
# "dont" and matches a catalogue entry spelled "Dont". Replacing them with a
# space would give "don t", which matches neither.
_APOSTROPHES = re.compile(r"['’ʼ´`]")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalise(value: str) -> str:
    """Reduce a title or artist name to a comparable key.

    Case-folded, accent-stripped, punctuation-free, with the release-variant
    noise removed. "Björk" and "Bjork" agree; "Idioteque - 2009 Remaster" and
    "Idioteque" agree; "Two Weeks" and "Two Weeks (feat. Someone)" agree.
    """
    text = unicodedata.normalize("NFKD", value)
    # Drop combining marks: this is what makes "Björk" == "Bjork".
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _TRAILING_NOISE.sub(" ", text)
    text = _TRAILING_DASH_NOISE.sub("", text)
    text = _APOSTROPHES.sub("", text)
    text = _PUNCTUATION.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip().casefold()


def match_key(artist_name: str, track_name: str) -> str:
    """The key both sides of the join are built from."""
    return f"{normalise(artist_name)}\x1f{normalise(track_name)}"
