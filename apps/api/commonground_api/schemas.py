"""Request and response models.

Kept apart from the ORM models on purpose: what the database stores and what the
API promises are different contracts, and letting a column rename become a
breaking API change is how that promise gets broken by accident. It is also the
line that stops `password_hash` ever being serialised into a response.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Long enough to matter, short enough that Argon2 cannot be used as a CPU
# amplifier by posting a megabyte password.
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=60)

    @field_validator("email")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        # The database has a CHECK that email = lower(email); normalising here
        # means a mixed-case signup succeeds rather than 500ing on the check.
        return value.lower()

    @field_validator("display_name")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("display name cannot be blank")
        return stripped


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    is_demo: bool
    onboarded_at: datetime | None
    created_at: datetime


# ------------------------------------------------------------------ taste --


class ArtistResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mbid: uuid.UUID
    name: str
    listen_count: int


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    is_genre: bool


class OnboardingRequest(BaseModel):
    """What a user picks at onboarding, with no Spotify involved.

    Both lists may be empty individually, but not together -- a profile with
    nothing in it produces recommendations from priors alone, and it is better
    to say so at the door than to silently make one.
    """

    artist_ids: list[int] = Field(default_factory=list, max_length=100)
    tag_ids: list[int] = Field(default_factory=list, max_length=50)

    @field_validator("artist_ids", "tag_ids")
    @classmethod
    def _dedupe(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(value))


class ProfileResponse(BaseModel):
    artists: list[ArtistResponse]
    tags: list[TagResponse]
    listen_count: int
    onboarded_at: datetime | None


# ---------------------------------------------------------------- imports --


class ImportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    format: str
    filename: str
    status: str
    rows_total: int
    rows_matched: int
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class ImportResultResponse(ImportResponse):
    """The detail an upload returns immediately after being processed.

    `skipped` is broken down by reason rather than totalled: "we dropped 412
    rows" invites no action, while "412 podcasts, 88 skips under 30s" explains
    itself.
    """

    skipped: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    match_rate: float


# --------------------------------------------------------------------- rooms --

MODES = ("consensus", "discovery", "fair_rotation")


class RoomCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    mode: str = Field(default="consensus")

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("a room needs a name")
        return stripped

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}")
        return value


class RoomMemberResponse(BaseModel):
    user_id: uuid.UUID
    display_name: str
    role: str
    joined_at: datetime
    # Surfaced because a member who has not onboarded is scored from priors
    # rather than from a prediction, and the room should be able to say so.
    onboarded: bool


class RoomResponse(BaseModel):
    id: uuid.UUID
    name: str
    mode: str
    status: str
    owner_id: uuid.UUID
    created_at: datetime
    member_count: int


class RoomDetailResponse(RoomResponse):
    members: list[RoomMemberResponse]


class InviteResponse(BaseModel):
    """The token is returned exactly once, at creation. Only its hash is stored."""

    token: str
    room_id: uuid.UUID
    expires_at: datetime
    max_uses: int


class PlaylistTrackResponse(BaseModel):
    id: int
    position: int
    recording_id: int
    title: str
    artist: str
    listen_url: str | None
    group_score: float
    member_scores: dict
    contributions: dict
    explanation: dict
    votes: int


class PlaylistResponse(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    mode: str
    engine_version: str
    seed: int
    params: dict
    generated_at: datetime
    duration_ms: int | None
    member_ids: list[str]
    tracks: list[PlaylistTrackResponse]


class VoteRequest(BaseModel):
    # 0 retracts a vote. Modelling "no opinion" as a value rather than as a
    # DELETE keeps one endpoint and one permission check for every vote change.
    value: int = Field(ge=-1, le=1)
