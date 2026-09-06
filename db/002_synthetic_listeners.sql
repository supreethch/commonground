-- Synthetic listeners: interaction history without an account behind it.
--
-- The demo catalogue has ~1,600 recordings and, before this, eight users. That
-- is not enough for collaborative filtering to mean anything: with eight
-- histories there is no co-occurrence to learn from, so every recommendation
-- would fall back to popularity and the whole engineering story would be
-- invisible in the product that is supposed to demonstrate it.
--
-- scripts/seed_listeners.py maps real ListenBrainz users (CC0) onto catalogue
-- recordings and writes them here. They own listens and nothing else: no
-- rooms, no votes, no login. Marked so they can never be mistaken for accounts,
-- and so a user count in the UI does not silently include them.

ALTER TABLE users ADD COLUMN is_synthetic boolean NOT NULL DEFAULT false;

-- Almost every query about real people filters these out, and the training
-- query wants exactly them, so both directions get an index.
CREATE INDEX users_synthetic_idx ON users (is_synthetic);

-- A synthetic listener has no usable credential. The password hash is a
-- sentinel that Argon2 cannot parse, so verification fails closed rather than
-- depending on nobody guessing an unset password.
ALTER TABLE users ADD CONSTRAINT users_synthetic_never_logs_in
    CHECK (NOT is_synthetic OR password_hash = 'synthetic-listener-no-login');
