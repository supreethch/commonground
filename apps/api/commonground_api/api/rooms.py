"""Rooms, invite links, playlist generation, voting and history."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from ..db import get_sessionmaker
from ..deps import CurrentUser, SessionDep, SettingsDep, WritableUser
from ..models import (
    Playlist,
    PlaylistTrack,
    RecRun,
    Room,
    RoomInvite,
    RoomMember,
    User,
    Vote,
)
from ..realtime import Event, get_broadcaster
from ..schemas import (
    InviteResponse,
    PlaylistResponse,
    PlaylistTrackResponse,
    RoomCreateRequest,
    RoomDetailResponse,
    RoomMemberResponse,
    RoomResponse,
    VoteRequest,
)
from ..security import decode_access_token, hash_token, new_invite_token
from ..services.recommender import recommender_service

router = APIRouter(prefix="/api/rooms", tags=["rooms"])

INVITE_TTL_DAYS = 14
MAX_MEMBERS = 12


# ------------------------------------------------------------------ helpers --


def _require_member(session: Session, room_id: uuid.UUID, user: User) -> Room:
    room = session.get(Room, room_id)
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="room not found")
    membership = session.get(RoomMember, (room_id, user.id))
    if membership is None:
        # 404 rather than 403: a non-member should not be able to discover that
        # a room id exists by the difference in status code.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="room not found")
    return room


def _require_owner(session: Session, room_id: uuid.UUID, user: User) -> Room:
    room = _require_member(session, room_id, user)
    if room.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="only the room owner can do that"
        )
    return room


def _members(session: Session, room_id: uuid.UUID) -> list[tuple[RoomMember, User]]:
    return list(
        session.execute(
            select(RoomMember, User)
            .join(User, User.id == RoomMember.user_id)
            .where(RoomMember.room_id == room_id)
            .order_by(RoomMember.joined_at, User.display_name)
        ).all()
    )


def _room_response(session: Session, room: Room) -> RoomDetailResponse:
    members = _members(session, room.id)
    return RoomDetailResponse(
        id=room.id,
        name=room.name,
        mode=room.mode,
        status=room.status,
        owner_id=room.owner_id,
        created_at=room.created_at,
        member_count=len(members),
        members=[
            RoomMemberResponse(
                user_id=user.id,
                display_name=user.display_name,
                role=membership.role,
                joined_at=membership.joined_at,
                onboarded=user.onboarded_at is not None,
            )
            for membership, user in members
        ],
    )


async def _emit(room_id: uuid.UUID, event_type: str, **payload) -> None:
    await get_broadcaster().publish(
        room_id, Event(type=event_type, room_id=str(room_id), payload=payload)
    )


# -------------------------------------------------------------------- rooms --


@router.post("", response_model=RoomDetailResponse, status_code=status.HTTP_201_CREATED)
def create_room(
    body: RoomCreateRequest, session: SessionDep, user: WritableUser
) -> RoomDetailResponse:
    room = Room(name=body.name, owner_id=user.id, mode=body.mode)
    session.add(room)
    session.flush()
    session.add(RoomMember(room_id=room.id, user_id=user.id, role="owner"))
    session.commit()
    return _room_response(session, room)


@router.get("", response_model=list[RoomResponse])
def list_rooms(session: SessionDep, user: CurrentUser) -> list[RoomResponse]:
    rows = session.execute(
        select(Room, func.count(RoomMember.user_id))
        .join(RoomMember, RoomMember.room_id == Room.id)
        .where(Room.id.in_(select(RoomMember.room_id).where(RoomMember.user_id == user.id)))
        .group_by(Room.id)
        .order_by(Room.created_at.desc())
    ).all()
    return [
        RoomResponse(
            id=room.id,
            name=room.name,
            mode=room.mode,
            status=room.status,
            owner_id=room.owner_id,
            created_at=room.created_at,
            member_count=count,
        )
        for room, count in rows
    ]


@router.get("/{room_id}", response_model=RoomDetailResponse)
def get_room(room_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> RoomDetailResponse:
    return _room_response(session, _require_member(session, room_id, user))


@router.delete("/{room_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_room(room_id: uuid.UUID, session: SessionDep, user: WritableUser) -> None:
    room = _require_owner(session, room_id, user)
    session.delete(room)
    session.commit()


# ------------------------------------------------------------------ invites --


@router.post("/{room_id}/invites", response_model=InviteResponse, status_code=201)
def create_invite(room_id: uuid.UUID, session: SessionDep, user: WritableUser) -> InviteResponse:
    room = _require_member(session, room_id, user)
    token, token_hash = new_invite_token()
    invite = RoomInvite(
        room_id=room.id,
        token_hash=token_hash,
        created_by=user.id,
        expires_at=datetime.now(UTC) + timedelta(days=INVITE_TTL_DAYS),
    )
    session.add(invite)
    session.commit()
    # The token itself is returned exactly once, here. Only its hash is stored,
    # so a leaked database cannot be used to join private rooms.
    return InviteResponse(
        token=token, room_id=room.id, expires_at=invite.expires_at, max_uses=invite.max_uses
    )


@router.post("/join/{token}", response_model=RoomDetailResponse)
async def join_room(token: str, session: SessionDep, user: WritableUser) -> RoomDetailResponse:
    invite = session.scalar(select(RoomInvite).where(RoomInvite.token_hash == hash_token(token)))
    now = datetime.now(UTC)
    if (
        invite is None
        or invite.revoked_at is not None
        or invite.expires_at <= now
        or invite.uses >= invite.max_uses
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="this invite is not valid"
        )

    room = session.get(Room, invite.room_id)
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="room not found")

    existing = session.get(RoomMember, (room.id, user.id))
    if existing is None:
        member_count = session.scalar(
            select(func.count()).select_from(RoomMember).where(RoomMember.room_id == room.id)
        )
        if member_count >= MAX_MEMBERS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"this room is full ({MAX_MEMBERS} members)",
            )
        session.add(RoomMember(room_id=room.id, user_id=user.id))
        # Only a join that actually added someone spends a use. Re-opening the
        # link you already used must not burn the invite for a friend.
        invite.uses += 1
        session.commit()
        await _emit(
            room.id,
            "member_joined",
            user_id=str(user.id),
            display_name=user.display_name,
        )

    return _room_response(session, room)


# --------------------------------------------------------------- playlists --


def _serialise_playlist(session: Session, playlist: Playlist) -> PlaylistResponse:
    rows = session.execute(
        text(
            """
            SELECT pt.id, pt.position, pt.group_score, pt.member_scores,
                   pt.contributions, pt.explanation,
                   r.id, r.title,
                   -- A lateral, not a join: a recording credited to two artists
                   -- would otherwise return two rows for one track, and the
                   -- playlist came back with a duplicated position.
                   (SELECT string_agg(a.name, ', ' ORDER BY ra.position, a.name)
                      FROM recording_artists ra
                      JOIN artists a ON a.id = ra.artist_id
                     WHERE ra.recording_id = r.id),
                   (SELECT url FROM recording_links l
                     WHERE l.recording_id = r.id ORDER BY l.source DESC, l.id LIMIT 1),
                   coalesce((SELECT sum(v.value) FROM votes v
                              WHERE v.playlist_track_id = pt.id), 0)
            FROM playlist_tracks pt
            JOIN recordings r ON r.id = pt.recording_id
            WHERE pt.playlist_id = :playlist
            ORDER BY pt.position
            """
        ),
        {"playlist": playlist.id},
    ).all()

    return PlaylistResponse(
        id=playlist.id,
        room_id=playlist.room_id,
        mode=playlist.mode,
        engine_version=playlist.engine_version,
        seed=playlist.seed,
        params=playlist.params,
        generated_at=playlist.generated_at,
        duration_ms=playlist.duration_ms,
        member_ids=[str(m) for m in playlist.member_ids],
        tracks=[
            PlaylistTrackResponse(
                id=row[0],
                position=row[1],
                group_score=row[2],
                member_scores=row[3],
                contributions=row[4],
                explanation=row[5],
                recording_id=row[6],
                title=row[7],
                artist=row[8],
                listen_url=row[9],
                votes=int(row[10]),
            )
            for row in rows
        ],
    )


@router.post("/{room_id}/playlist", response_model=PlaylistResponse, status_code=201)
async def generate_playlist(
    room_id: uuid.UUID,
    session: SessionDep,
    # CurrentUser, not WritableUser: generating a playlist is the demo. It
    # writes a playlist row, not a change to anyone's profile, so a shared demo
    # login cannot spoil the next visitor's view by using it.
    user: CurrentUser,
    k: int = Query(default=20, ge=1, le=50),
) -> PlaylistResponse:
    room = _require_member(session, room_id, user)
    members = _members(session, room_id)
    member_ids = [membership.user_id for membership, _ in members]

    # Seeded from the room so regenerating is reproducible, and salted with the
    # playlist count so "regenerate" actually produces something new rather than
    # the same list again.
    previous = session.scalar(
        select(func.count()).select_from(Playlist).where(Playlist.room_id == room_id)
    )
    seed = (int(room.id.int % 1_000_003) + previous) % (2**31)

    generated = recommender_service.generate(
        session,
        member_ids=member_ids,
        member_names=[user.display_name for _, user in members],
        mode_name=room.mode,
        k=k,
        seed=seed,
    )
    if not generated.tracks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "could not build a playlist for this room -- the catalogue has "
                "nothing left that every member could hear"
            ),
        )

    playlist = Playlist(
        room_id=room.id,
        mode=generated.mode,
        engine_version=generated.engine_version,
        params={
            **generated.params,
            "tau_applied": generated.tau_applied,
            "veto_relaxed": generated.veto_relaxed,
        },
        seed=generated.seed,
        member_ids=member_ids,
        duration_ms=generated.duration_ms,
    )
    session.add(playlist)
    session.flush()

    for track in generated.tracks:
        session.add(
            PlaylistTrack(
                playlist_id=playlist.id,
                recording_id=track.recording_id,
                position=track.position,
                group_score=track.group_score,
                member_scores=track.member_scores,
                contributions=track.contributions,
                explanation=track.explanation,
            )
        )

    # Every generation is recorded, so docs/measurements.md reports timings from
    # rows rather than from a good run someone remembered.
    session.add(
        RecRun(
            room_id=room.id,
            playlist_id=playlist.id,
            requested_by=user.id,
            member_count=len(member_ids),
            candidate_count=generated.candidate_count,
            duration_ms=generated.duration_ms,
            engine_version=generated.engine_version,
        )
    )
    session.commit()

    await _emit(
        room.id,
        "playlist_generated",
        playlist_id=str(playlist.id),
        mode=playlist.mode,
        track_count=len(generated.tracks),
        veto_relaxed=generated.veto_relaxed,
    )
    return _serialise_playlist(session, playlist)


@router.get("/{room_id}/playlist", response_model=PlaylistResponse)
def latest_playlist(room_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> PlaylistResponse:
    _require_member(session, room_id, user)
    playlist = session.scalar(
        select(Playlist)
        .where(Playlist.room_id == room_id)
        .order_by(Playlist.generated_at.desc())
        .limit(1)
    )
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="this room has no playlist yet"
        )
    return _serialise_playlist(session, playlist)


@router.get("/{room_id}/history", response_model=list[PlaylistResponse])
def playlist_history(
    room_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[PlaylistResponse]:
    _require_member(session, room_id, user)
    playlists = session.scalars(
        select(Playlist)
        .where(Playlist.room_id == room_id)
        .order_by(Playlist.generated_at.desc())
        .limit(limit)
    ).all()
    return [_serialise_playlist(session, playlist) for playlist in playlists]


# -------------------------------------------------------------------- votes --


@router.post("/{room_id}/tracks/{track_id}/vote", status_code=status.HTTP_200_OK)
async def cast_vote(
    room_id: uuid.UUID,
    track_id: int,
    body: VoteRequest,
    session: SessionDep,
    # Also CurrentUser: a vote is one row per member per track, and voting is
    # half of what a visitor comes to try.
    user: CurrentUser,
) -> dict:
    _require_member(session, room_id, user)

    track = session.get(PlaylistTrack, track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="track not found")
    playlist = session.get(Playlist, track.playlist_id)
    if playlist is None or playlist.room_id != room_id:
        # The track exists but belongs to another room. 404 rather than 403 for
        # the same reason as above.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="track not found")

    if body.value == 0:
        session.execute(
            delete(Vote).where(Vote.playlist_track_id == track_id, Vote.user_id == user.id)
        )
    else:
        existing = session.get(Vote, (track_id, user.id))
        if existing is None:
            session.add(Vote(playlist_track_id=track_id, user_id=user.id, value=body.value))
        else:
            # Changing your mind updates the row. The primary key is what makes
            # a refresh-and-click-again harmless.
            existing.value = body.value
    session.commit()

    total = session.scalar(
        select(func.coalesce(func.sum(Vote.value), 0)).where(Vote.playlist_track_id == track_id)
    )
    await _emit(
        room_id,
        "vote",
        track_id=track_id,
        user_id=str(user.id),
        value=body.value,
        total=int(total),
    )
    return {"track_id": track_id, "value": body.value, "total": int(total)}


# ---------------------------------------------------------------- websocket --


def get_socket_session_factory():
    """Session factory for the socket handshake.

    A dependency rather than a direct call so tests can point it at their own
    transaction -- and a *factory* rather than a session because the socket must
    not hold a pooled connection for its entire lifetime. Authentication needs
    the database for a few milliseconds; the connection goes back to the pool
    before the long-lived loop starts. With pool_size=5 the alternative is that
    ten open rooms exhaust the pool and the API stops answering requests.
    """
    return get_sessionmaker()


SocketSessionFactory = Annotated[Any, Depends(get_socket_session_factory)]


def _load_socket_user(session: Session, user_id: uuid.UUID, room_id: uuid.UUID) -> User | None:
    user = session.get(User, user_id)
    if user is None or session.get(RoomMember, (room_id, user_id)) is None:
        return None
    session.expunge(user)
    return user


@router.websocket("/{room_id}/ws")
async def room_socket(
    websocket: WebSocket,
    room_id: uuid.UUID,
    factory: SocketSessionFactory,
    settings: SettingsDep,
) -> None:
    # Settings arrive as a dependency, not a direct get_settings() call. Reading
    # them directly bypassed the test override, so the socket verified tokens
    # against a different signing key than the one that issued them and refused
    # every legitimate member with "not authenticated".
    # Authenticate *before* accepting. Accepting first and closing after would
    # complete the handshake for an unauthenticated caller, which reads to a
    # client as "connected, then dropped" rather than "refused" -- and briefly
    # registers a socket nobody should have.
    #
    # The token arrives as a query parameter because a browser cannot set
    # headers on a WebSocket handshake. That puts it in server logs, so it is a
    # short-lived access token and never the refresh token, and this socket is
    # read-only: possession of it grants no writes.
    token = websocket.query_params.get("token")
    user_id = decode_access_token(settings, token) if token else None
    if user_id is None:
        await websocket.close(code=4401, reason="not authenticated")
        return

    with factory() as session:
        user = _load_socket_user(session, user_id, room_id)
    if user is None:
        await websocket.close(code=4403, reason="not a member of this room")
        return

    await websocket.accept()

    broadcaster = get_broadcaster()
    # Bounded: a client that stops reading drops events rather than growing the
    # queue until the process runs out of memory. They re-sync over REST.
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
    await broadcaster.subscribe(room_id, queue)

    await websocket.send_text(
        Event(
            type="connected",
            room_id=str(room_id),
            payload={"user_id": str(user.id), "transport": broadcaster.name},
        ).encode()
    )

    async def pump() -> None:
        while True:
            event = await queue.get()
            await websocket.send_text(event.encode())

    sender = asyncio.create_task(pump())
    try:
        while True:
            # The socket carries no commands -- state changes go over REST -- so
            # anything received is discarded. Reading is still necessary to
            # notice a disconnect.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sender
        await broadcaster.unsubscribe(room_id, queue)
