-- Persist JWT revocation state for logout and password changes.
ALTER TABLE users
    ADD COLUMN token_version integer NOT NULL DEFAULT 0
    CHECK (token_version >= 0);
