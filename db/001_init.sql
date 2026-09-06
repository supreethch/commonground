-- CommonGround initial schema.
--
-- Two id conventions, on purpose:
--
--   * Catalogue tables (artists, recordings, tags) use bigint identity keys and
--     carry the MusicBrainz UUID alongside. The engine needs dense contiguous
--     integer indices to address rows and columns of its matrices, and deriving
--     those from UUIDs on every build is wasted work. The MBID stays as the
--     stable external identity.
--   * User-facing tables (users, rooms, playlists) use UUIDs, because their ids
--     appear in URLs and invite links and a sequential id there leaks how many
--     users and rooms exist.
--
-- gen_random_uuid() is core PostgreSQL since 13; no extension needed.

-- ---------------------------------------------------------------- accounts --

CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Stored already lower-cased by the API so the unique constraint is a real
    -- one; a CHECK keeps a stray direct INSERT from creating a duplicate that
    -- only differs in case.
    email           text        NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash   text        NOT NULL,
    display_name    text        NOT NULL CHECK (length(display_name) BETWEEN 1 AND 60),
    -- Demo accounts are seeded so a recruiter can sign in without registering.
    -- They are marked so destructive operations can refuse to touch them and so
    -- they can be excluded from any evaluation.
    is_demo         boolean     NOT NULL DEFAULT false,
    onboarded_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Refresh tokens are stored hashed. A leaked database should not hand out live
-- sessions, and this table is the one an attacker would read first.
CREATE TABLE refresh_tokens (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  text        NOT NULL UNIQUE,
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX refresh_tokens_user_idx ON refresh_tokens (user_id) WHERE revoked_at IS NULL;

-- --------------------------------------------------------------- catalogue --

CREATE TABLE artists (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mbid        uuid        NOT NULL UNIQUE,
    name        text        NOT NULL,
    sort_name   text,
    country     char(2),
    -- Denormalised from listen data at build time. Used by the popularity
    -- component and by candidate generation, both of which run per request.
    listen_count bigint     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artists_name_idx ON artists (lower(name));

CREATE TABLE recordings (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mbid               uuid        NOT NULL UNIQUE,
    title              text        NOT NULL,
    length_ms          integer     CHECK (length_ms IS NULL OR length_ms > 0),
    first_release_year smallint,
    listen_count       bigint      NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX recordings_title_idx ON recordings (lower(title));
CREATE INDEX recordings_popularity_idx ON recordings (listen_count DESC);

CREATE TABLE recording_artists (
    recording_id bigint   NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    artist_id    bigint   NOT NULL REFERENCES artists(id)    ON DELETE CASCADE,
    position     smallint NOT NULL DEFAULT 0,
    PRIMARY KEY (recording_id, artist_id)
);
CREATE INDEX recording_artists_artist_idx ON recording_artists (artist_id);

-- Tag vocabulary. The tag *names* come from MusicBrainz supplementary data,
-- which is CC BY-NC-SA 3.0 -- see docs/data-sources.md. Nothing derived from
-- these tables is committed to this repository.
CREATE TABLE tags (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL UNIQUE,
    -- MusicBrainz distinguishes a curated genre list from free-form user tags.
    -- Genres are the stronger signal, so the engine can weight them separately.
    is_genre boolean NOT NULL DEFAULT false
);

CREATE TABLE artist_tags (
    artist_id bigint           NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    tag_id    bigint           NOT NULL REFERENCES tags(id)    ON DELETE CASCADE,
    weight    double precision NOT NULL CHECK (weight >= 0),
    PRIMARY KEY (artist_id, tag_id)
);
CREATE INDEX artist_tags_tag_idx ON artist_tags (tag_id);

CREATE TABLE recording_tags (
    recording_id bigint           NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    tag_id       bigint           NOT NULL REFERENCES tags(id)       ON DELETE CASCADE,
    weight       double precision NOT NULL CHECK (weight >= 0),
    PRIMARY KEY (recording_id, tag_id)
);
CREATE INDEX recording_tags_tag_idx ON recording_tags (tag_id);

-- Outbound playback links. CommonGround hosts no audio.
--
-- A spot check found MusicBrainz recording-level URL relations to be sparse
-- (docs/data-sources.md), so most rows here are deterministic search URLs built
-- from artist and title. 'source' records which kind a row is, so the measured
-- share of real relations can be reported honestly rather than estimated.
CREATE TABLE recording_links (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recording_id bigint NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    url          text   NOT NULL,
    provider     text   NOT NULL,
    source       text   NOT NULL CHECK (source IN ('musicbrainz_relation', 'search_url')),
    UNIQUE (recording_id, url)
);

-- --------------------------------------------------------- taste and input --

-- What a member told us directly at onboarding, plus what we inferred from an
-- import. 'source' matters because explicit choices should outrank inferred
-- ones and the explanation text distinguishes them.
CREATE TABLE profile_artists (
    user_id   uuid             NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    artist_id bigint           NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    weight    double precision NOT NULL DEFAULT 1.0,
    source    text             NOT NULL CHECK (source IN ('onboarding', 'import', 'feedback')),
    created_at timestamptz     NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, artist_id)
);

CREATE TABLE profile_tags (
    user_id uuid             NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tag_id  bigint           NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    weight  double precision NOT NULL DEFAULT 1.0,
    source  text             NOT NULL CHECK (source IN ('onboarding', 'import', 'feedback')),
    PRIMARY KEY (user_id, tag_id)
);

CREATE TABLE imports (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    format       text        NOT NULL CHECK (format IN ('spotify_gdpr', 'listenbrainz', 'lastfm_csv')),
    filename     text        NOT NULL,
    status       text        NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed')),
    rows_total   integer     NOT NULL DEFAULT 0,
    -- Match rate is the number worth surfacing to the user: "we matched 812 of
    -- your 1,004 plays" is honest, and a silently low rate is the failure mode
    -- of every history importer.
    rows_matched integer     NOT NULL DEFAULT 0,
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

CREATE TABLE listens (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      uuid        NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
    recording_id bigint      NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    listened_at  timestamptz NOT NULL,
    import_id    uuid        REFERENCES imports(id) ON DELETE SET NULL,
    -- The same play must not be counted twice when a user re-uploads an
    -- overlapping export, which they will.
    UNIQUE (user_id, recording_id, listened_at)
);
CREATE INDEX listens_user_idx ON listens (user_id, listened_at DESC);

-- Explicit signal. Kept separate from listens: a like is a statement, a play is
-- a behaviour, and the engine weights them differently.
CREATE TABLE interactions (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      uuid        NOT NULL REFERENCES users(id)      ON DELETE CASCADE,
    recording_id bigint      NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    kind         text        NOT NULL CHECK (kind IN ('like', 'dislike', 'skip', 'play')),
    room_id      uuid,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX interactions_user_idx      ON interactions (user_id, created_at DESC);
CREATE INDEX interactions_recording_idx ON interactions (recording_id);

-- ------------------------------------------------------------------- rooms --

CREATE TABLE rooms (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text        NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    owner_id   uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mode       text        NOT NULL CHECK (mode IN ('consensus', 'discovery', 'fair_rotation')),
    status     text        NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    settings   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rooms_owner_idx ON rooms (owner_id, created_at DESC);

CREATE TABLE room_members (
    room_id   uuid        NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id   uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role      text        NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
    joined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (room_id, user_id)
);
CREATE INDEX room_members_user_idx ON room_members (user_id);

-- Invite tokens are hashed for the same reason refresh tokens are: the token in
-- the link is a bearer credential for joining a room.
CREATE TABLE room_invites (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id    uuid        NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    token_hash text        NOT NULL UNIQUE,
    created_by uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    max_uses   integer     NOT NULL DEFAULT 20 CHECK (max_uses > 0),
    uses       integer     NOT NULL DEFAULT 0  CHECK (uses >= 0),
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX room_invites_room_idx ON room_invites (room_id);

-- --------------------------------------------------------------- playlists --

-- Everything needed to regenerate a playlist byte-for-byte is stored on the row:
-- engine version, parameters and seed. A saved playlist whose ranking cannot be
-- reproduced cannot have its explanations checked, so this is not bookkeeping.
CREATE TABLE playlists (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    room_id        uuid        NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    mode           text        NOT NULL CHECK (mode IN ('consensus', 'discovery', 'fair_rotation')),
    engine_version text        NOT NULL,
    params         jsonb       NOT NULL,
    seed           bigint      NOT NULL,
    -- The members present at generation time. Membership changes later must not
    -- silently rewrite the history of why a track was chosen.
    member_ids     uuid[]      NOT NULL,
    generated_at   timestamptz NOT NULL DEFAULT now(),
    duration_ms    integer
);
CREATE INDEX playlists_room_idx ON playlists (room_id, generated_at DESC);

CREATE TABLE playlist_tracks (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    playlist_id  uuid             NOT NULL REFERENCES playlists(id)  ON DELETE CASCADE,
    recording_id bigint           NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    position     smallint         NOT NULL CHECK (position >= 0),
    group_score  double precision NOT NULL,
    -- Per-member satisfaction, {user_id: s(u,i)}. Kept rather than recomputed:
    -- it is what the fairness view renders and what an explanation is checked
    -- against.
    member_scores jsonb           NOT NULL,
    -- The named score contributions that produced group_score, and the rendered
    -- reason built from them.
    contributions jsonb           NOT NULL,
    explanation   jsonb           NOT NULL,
    UNIQUE (playlist_id, position)
);

CREATE TABLE votes (
    playlist_track_id bigint      NOT NULL REFERENCES playlist_tracks(id) ON DELETE CASCADE,
    user_id           uuid        NOT NULL REFERENCES users(id)           ON DELETE CASCADE,
    value             smallint    NOT NULL CHECK (value IN (-1, 1)),
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- One vote per member per track; changing your mind updates the row.
    PRIMARY KEY (playlist_track_id, user_id)
);

-- Timing for every generation, so docs/measurements.md is written from records
-- rather than from memory of a good run.
CREATE TABLE rec_runs (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    room_id        uuid        NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    playlist_id    uuid        REFERENCES playlists(id) ON DELETE SET NULL,
    requested_by   uuid        REFERENCES users(id) ON DELETE SET NULL,
    member_count   smallint    NOT NULL,
    candidate_count integer    NOT NULL,
    duration_ms    integer     NOT NULL,
    engine_version text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rec_runs_room_idx ON rec_runs (room_id, created_at DESC);
