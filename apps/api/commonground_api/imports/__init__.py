"""Parsers for user-supplied listening history.

Pure functions over bytes. Nothing here touches a database or the network, so
every format quirk is testable against a fixture instead of an upload.
"""

from .parsers import (
    FORMATS,
    ParsedListen,
    ParseResult,
    UnknownFormatError,
    detect_format,
    parse,
    parse_lastfm_csv,
    parse_listenbrainz,
    parse_spotify_gdpr,
)

__all__ = [
    "FORMATS",
    "ParseResult",
    "ParsedListen",
    "UnknownFormatError",
    "detect_format",
    "parse",
    "parse_lastfm_csv",
    "parse_listenbrainz",
    "parse_spotify_gdpr",
]
