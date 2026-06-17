-- Persistence schema for opentrons_control.
--
-- users         console accounts and roles
-- invite_codes  single-use codes that authorise account creation
-- secrets       credentials held as application-encrypted ciphertext
-- robots        connection details for each OT


-- users — console accounts.
CREATE TABLE users (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,                -- PBKDF2 string: "sha256$<salt>$<hash>"
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ                   -- NULL marks an active account
);

-- A name is unique among active accounts; a soft-deleted name is free to reuse.
CREATE UNIQUE INDEX users_name_active_uq
    ON users (name)
    WHERE deleted_at IS NULL;


-- invite_codes — single-use registration codes. An unused code has used_by NULL;
-- registration consumes it by stamping used_by and used_at. target_role is the
-- role the new account receives.
CREATE TABLE invite_codes (
    code         TEXT PRIMARY KEY,
    target_role  TEXT NOT NULL DEFAULT 'user' CHECK (target_role IN ('admin', 'user')),
    used_by      INTEGER REFERENCES users(id),  -- NULL while the code is unused
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at      TIMESTAMPTZ
);


-- secrets — credentials stored as opaque ciphertext; plaintext never reaches
-- the database.
CREATE TABLE secrets (
    name        TEXT PRIMARY KEY,               -- lookup key, e.g. 'ot3_ssh_key'
    ciphertext  BYTEA NOT NULL,                 -- Fernet token, opaque to the database
    kind        TEXT NOT NULL DEFAULT 'other',  -- 'ssh_key' | 'git_deploy' | 'other'
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- robots — one OT per row. key_name points at the secrets entry holding this
-- robot's SSH private key.
CREATE TABLE robots (
    robot_id    TEXT PRIMARY KEY,               -- logical identifier, e.g. 'ot-3'
    host        TEXT NOT NULL,                  -- address on the robot subnet
    ssh_user    TEXT NOT NULL DEFAULT 'root',   -- 'user' is a reserved word
    agent_port  INTEGER NOT NULL DEFAULT 9000,
    key_name    TEXT REFERENCES secrets(name) ON DELETE RESTRICT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);