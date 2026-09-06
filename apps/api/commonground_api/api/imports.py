"""Upload and resolve a listening-history export."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..deps import CurrentUser, SessionDep, SettingsDep, WritableUser
from ..imports import FORMATS, ParseResult, UnknownFormatError, detect_format, parse
from ..imports.matching import match_key
from ..models import Artist, Import, Listen, ProfileArtist, Recording, RecordingArtist
from ..schemas import ImportResponse, ImportResultResponse

router = APIRouter(prefix="/api/imports", tags=["imports"])

# Listens per artist needed before an import contributes that artist to the
# taste profile. One play of something is not evidence of taste; it is evidence
# of a shuffle.
PROFILE_ARTIST_MIN_LISTENS = 3
PROFILE_ARTIST_LIMIT = 60


def _catalogue_index(session) -> dict[str, int]:
    """Map normalised 'artist\\x1ftitle' to recording id.

    Built in one pass rather than a query per row: a 5,000-row export would
    otherwise be 5,000 round trips, and at that point the upload times out
    rather than merely being slow.
    """
    rows = session.execute(
        select(Recording.id, Recording.title, Artist.name)
        .join(RecordingArtist, RecordingArtist.recording_id == Recording.id)
        .join(Artist, Artist.id == RecordingArtist.artist_id)
    ).all()
    index: dict[str, int] = {}
    for recording_id, title, artist_name in rows:
        index.setdefault(match_key(artist_name, title), recording_id)
    return index


def _mbid_index(session) -> dict[str, int]:
    return {
        str(mbid): pk for mbid, pk in session.execute(select(Recording.mbid, Recording.id)).all()
    }


def _store(session, user_id: uuid.UUID, import_id: uuid.UUID, result: ParseResult) -> int:
    """Insert resolved listens, returning how many rows were matched."""
    by_mbid = _mbid_index(session)
    by_name = _catalogue_index(session)

    rows: list[dict] = []
    for listen in result.listens:
        recording_id = None
        if listen.recording_mbid:
            recording_id = by_mbid.get(listen.recording_mbid)
        if recording_id is None:
            recording_id = by_name.get(match_key(listen.artist_name, listen.track_name))
        if recording_id is None:
            continue
        rows.append(
            {
                "user_id": user_id,
                "recording_id": recording_id,
                "listened_at": listen.listened_at,
                "import_id": import_id,
            }
        )

    if not rows:
        return 0

    # ON CONFLICT DO NOTHING rather than a pre-check: re-uploading an
    # overlapping export is normal, and the unique key is what makes that safe.
    session.execute(pg_insert(Listen).values(rows).on_conflict_do_nothing())
    return len(rows)


def _profile_from_listens(session, user_id: uuid.UUID) -> None:
    """Promote frequently-played artists into the taste profile.

    source='import' keeps these distinct from onboarding picks, so re-running
    onboarding does not erase them and the explanation text can say whether a
    preference was stated or inferred.
    """
    counted = (
        select(RecordingArtist.artist_id, func.count().label("plays"))
        .join(Listen, Listen.recording_id == RecordingArtist.recording_id)
        .where(Listen.user_id == user_id)
        .group_by(RecordingArtist.artist_id)
        .having(func.count() >= PROFILE_ARTIST_MIN_LISTENS)
        .order_by(func.count().desc())
        .limit(PROFILE_ARTIST_LIMIT)
    )
    rows = session.execute(counted).all()
    if not rows:
        return

    most = max(plays for _, plays in rows)
    for artist_id, plays in rows:
        session.execute(
            pg_insert(ProfileArtist)
            .values(
                user_id=user_id,
                artist_id=artist_id,
                # Normalised against the user's own most-played artist, so a
                # heavy listener and a light one produce comparable weights.
                weight=round(plays / most, 4),
                source="import",
            )
            .on_conflict_do_update(
                index_elements=["user_id", "artist_id"],
                set_={"weight": round(plays / most, 4), "source": "import"},
            )
        )


@router.post("", response_model=ImportResultResponse, status_code=status.HTTP_201_CREATED)
async def upload_import(
    session: SessionDep,
    settings: SettingsDep,
    user: WritableUser,
    file: UploadFile = File(...),
    export_format: str = Form(default="auto"),
) -> ImportResultResponse:
    payload = await file.read(settings.max_import_bytes + 1)
    if len(payload) > settings.max_import_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file larger than {settings.max_import_bytes // (1024 * 1024)}MB",
        )
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="file is empty"
        )

    if export_format == "auto":
        try:
            export_format = detect_format(payload)
        except UnknownFormatError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"could not recognise this file: {exc}",
            ) from exc
    elif export_format not in FORMATS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unsupported format; expected one of {list(FORMATS)}",
        )

    record = Import(
        user_id=user.id,
        format=export_format,
        filename=(file.filename or "upload")[:255],
        status="running",
    )
    session.add(record)
    session.flush()

    try:
        result = parse(payload, export_format)
    except UnknownFormatError as exc:
        record.status = "failed"
        record.error = str(exc)
        record.finished_at = datetime.now(UTC)
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    matched = _store(session, user.id, record.id, result)
    _profile_from_listens(session, user.id)

    record.rows_total = len(result.listens) + result.total_skipped
    record.rows_matched = matched
    record.status = "done"
    record.finished_at = datetime.now(UTC)
    session.commit()

    return ImportResultResponse(
        **ImportResponse.model_validate(record).model_dump(),
        skipped=result.skipped,
        warnings=result.warnings,
        # Reported against parseable listens, not the raw row count: a file that
        # is 90% podcasts should not read as a 10% match rate.
        match_rate=round(matched / len(result.listens), 4) if result.listens else 0.0,
    )


@router.get("", response_model=list[ImportResponse])
def list_imports(session: SessionDep, user: CurrentUser) -> list[Import]:
    return list(
        session.scalars(
            select(Import).where(Import.user_id == user.id).order_by(Import.created_at.desc())
        )
    )
