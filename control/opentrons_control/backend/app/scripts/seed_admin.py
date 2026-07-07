"""
Interactive admin bootstrap.

Ensures at least one active admin account exists. If one already does, the
script reports it and exits without changes, so it is safe to run on every
launch. If none exists and a terminal is attached, it prompts for a username
and password and creates the account.

This is the one path that creates an account without redeeming an invite — the
first admin has no issuer to redeem from, so it is the self-signed root of
trust. It creates the account through the shared primitive (the same
users/insert.sql + hash_password that /register uses) and waits for the
database through the shared wait_for_db (the same guard the app uses at
startup), so there is one way to write a user and one way to wait for the DB.

Intended to run inside the backend container, which carries database access on
the internal network and the application's password hashing:

    docker compose exec backend python -m opentrons_control.backend.app.scripts.seed_admin
"""

import getpass
import sys

from sqlalchemy import create_engine, text

from opentrons_control.backend.app.settings.config import settings
from opentrons_control.backend.app.security import hash_password
from opentrons_control.backend.app.db.db_session import wait_for_db
from opentrons_control.backend.app.db.runner import load_sql

MIN_PASSWORD_LEN = 8


def _admin_exists(engine) -> bool:
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM users "
                "WHERE role = 'admin' AND deleted_at IS NULL"
            )
        ).scalar()
    return bool(count)


def _prompt_admin() -> tuple[str, str]:
    name = input("Admin username: ").strip()
    if not name:
        print("Username cannot be empty.")
        sys.exit(1)
    while True:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match, try again.")
            continue
        if len(password) < MIN_PASSWORD_LEN:
            print(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
            continue
        return name, password


def main() -> None:
    # Standalone script: own engine, shared wait. wait_for_db returns it ready,
    # and a successful probe means initdb finished, so users is queryable below.
    engine = wait_for_db(create_engine(settings.database_url))

    if _admin_exists(engine):
        print("Admin account already exists. Skipping.")
        return

    if not sys.stdin.isatty():
        print("No admin account exists and no terminal is attached to create one.")
        print("Run: docker compose exec backend "
              "python -m opentrons_control.backend.app.scripts.seed_admin")
        sys.exit(1)

    name, password = _prompt_admin()
    with engine.begin() as conn:
        conn.execute(
            text(load_sql("users/insert.sql")),
            {"name": name, "role": "admin", "password_hash": hash_password(password)},
        )
    print(f"Admin account '{name}' created.")


if __name__ == "__main__":
    main()