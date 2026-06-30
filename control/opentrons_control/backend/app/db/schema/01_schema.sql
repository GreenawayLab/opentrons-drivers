-- Schema for the control-plane database.
--
-- Mounted into the postgres image at /docker-entrypoint-initdb.d and run once,
-- on first initialisation of an empty data volume. To re-apply after editing,
-- the volume must be recreated (docker compose down -v), since initdb scripts
-- run only on a fresh data directory.
--
-- Reconstructed from the application's queries; column types and constraints
-- match how the code reads and writes each table.

-- Users: login by name, soft-deleted via deleted_at, role drives gating.
-- id is integer (security.create_token(user_id: int)); IDENTITY auto-fills it
-- so seed_admin can INSERT (name, role, password_hash) without an id.
CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT        NOT NULL,
    password_hash TEXT        NOT NULL,
    role          TEXT        NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One active account per name; a soft-deleted name can be reused.
CREATE UNIQUE INDEX users_name_active_idx ON users (name) WHERE deleted_at IS NULL;

-- Robots: registry rows. upsert never sets `enabled`, so new rows default
-- enabled and a re-save preserves the existing value. key_name is nullable
-- (a robot may have no key linked yet) and references a secrets.name.
CREATE TABLE robots (
    robot_id   TEXT        PRIMARY KEY,
    host       TEXT        NOT NULL,
    ssh_user   TEXT        NOT NULL DEFAULT 'root',
    agent_port INTEGER     NOT NULL DEFAULT 9000,
    key_name   TEXT,
    enabled    BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Secrets: Fernet ciphertext, decrypted only in memory by the vault.
-- ciphertext is BYTEA: put_secret binds fernet.encrypt(...) (bytes) and
-- get_secret does bytes(row["ciphertext"]) — both require binary storage.
CREATE TABLE secrets (
    name       TEXT        PRIMARY KEY,
    ciphertext BYTEA       NOT NULL,
    kind       TEXT        NOT NULL DEFAULT 'other',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);