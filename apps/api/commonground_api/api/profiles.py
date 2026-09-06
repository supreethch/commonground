"""Catalogue browsing and taste onboarding.

This is the Spotify-free path: a user builds a taste profile by picking artists
and genres from the local catalogue, with no OAuth, no third-party account and
no restricted endpoints. Importing a history file (see api/imports.py) is an
optional accelerator, never a requirement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, func, select

from ..deps import CurrentUser, SessionDep, WritableUser
from ..models import Artist, ArtistTag, Listen, ProfileArtist, ProfileTag, Tag
from ..schemas import (
    ArtistResponse,
    OnboardingRequest,
    ProfileResponse,
    TagResponse,
)

router = APIRouter(prefix="/api", tags=["profile"])


@router.get("/artists", response_model=list[ArtistResponse])
def list_artists(
    session: SessionDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[Artist]:
    """Search or browse artists for the onboarding picker.

    With no query this returns the most-listened artists, which is what an empty
    onboarding screen should show -- a blank search box asking someone to recall
    an artist name from nothing is the worst possible first screen.
    """
    statement = select(Artist)
    if q:
        # ILIKE with a leading wildcard cannot use a btree index. At catalogue
        # sizes here that is irrelevant; if the catalogue grows past a few tens
        # of thousands this wants a trigram index, and the measurement should
        # come before the index.
        statement = statement.where(Artist.name.ilike(f"%{q}%"))
    statement = statement.order_by(Artist.listen_count.desc(), Artist.id).limit(limit)
    return list(session.scalars(statement))


@router.get("/tags", response_model=list[TagResponse])
def list_tags(
    session: SessionDep,
    genres_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 60,
) -> list[Tag]:
    """Genres for the onboarding picker, most-used first.

    Ordered by how many artists carry the tag rather than alphabetically: the
    first screen should offer "rock" and "hip hop", not "abstract idm".
    """
    usage = (
        select(ArtistTag.tag_id.label("tag_id"), func.count().label("uses"))
        .group_by(ArtistTag.tag_id)
        .subquery()
    )
    statement = (
        select(Tag)
        .join(usage, Tag.id == usage.c.tag_id)
        .order_by(usage.c.uses.desc(), Tag.name)
        .limit(limit)
    )
    if genres_only:
        statement = statement.where(Tag.is_genre.is_(True))
    return list(session.scalars(statement))


@router.get("/profile", response_model=ProfileResponse)
def get_profile(session: SessionDep, user: CurrentUser) -> ProfileResponse:
    artists = list(
        session.scalars(
            select(Artist)
            .join(ProfileArtist, ProfileArtist.artist_id == Artist.id)
            .where(ProfileArtist.user_id == user.id)
            .order_by(ProfileArtist.weight.desc(), Artist.name)
        )
    )
    tags = list(
        session.scalars(
            select(Tag)
            .join(ProfileTag, ProfileTag.tag_id == Tag.id)
            .where(ProfileTag.user_id == user.id)
            .order_by(ProfileTag.weight.desc(), Tag.name)
        )
    )
    listen_count = session.scalar(
        select(func.count()).select_from(Listen).where(Listen.user_id == user.id)
    )
    return ProfileResponse(
        artists=[ArtistResponse.model_validate(a) for a in artists],
        tags=[TagResponse.model_validate(t) for t in tags],
        listen_count=listen_count or 0,
        onboarded_at=user.onboarded_at,
    )


@router.put("/profile/onboarding", response_model=ProfileResponse)
def set_onboarding(
    body: OnboardingRequest, session: SessionDep, user: WritableUser
) -> ProfileResponse:
    """Replace the explicit part of a taste profile.

    Only rows with source='onboarding' are replaced. Anything learned from an
    import or from feedback survives, so re-running onboarding cannot silently
    erase a history the user uploaded.
    """
    if not body.artist_ids and not body.tag_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pick at least one artist or genre",
        )

    known_artists = set(session.scalars(select(Artist.id).where(Artist.id.in_(body.artist_ids))))
    unknown = set(body.artist_ids) - known_artists
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown artist ids: {sorted(unknown)}",
        )

    known_tags = set(session.scalars(select(Tag.id).where(Tag.id.in_(body.tag_ids))))
    unknown_tags = set(body.tag_ids) - known_tags
    if unknown_tags:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown tag ids: {sorted(unknown_tags)}",
        )

    session.execute(
        delete(ProfileArtist).where(
            ProfileArtist.user_id == user.id, ProfileArtist.source == "onboarding"
        )
    )
    session.execute(
        delete(ProfileTag).where(ProfileTag.user_id == user.id, ProfileTag.source == "onboarding")
    )
    for artist_id in body.artist_ids:
        session.add(
            ProfileArtist(user_id=user.id, artist_id=artist_id, weight=1.0, source="onboarding")
        )
    for tag_id in body.tag_ids:
        session.add(ProfileTag(user_id=user.id, tag_id=tag_id, weight=1.0, source="onboarding"))

    user.onboarded_at = datetime.now(UTC)
    session.commit()
    return get_profile(session, user)
