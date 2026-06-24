"""
Interactive admin bootstrap.

Ensures at least one active admin account exists. If one already does, the
script reports it and exits without changes, so it is safe to run on every
launch. If none exists and a terminal is attached, it prompts for a username
and password and creates the account.

Intended to run inside the backend container, which carries database access
on the internal network and the application's password hashing:

    docker compose exec backend python -m app.scripts.seed_admin
"""

import getpass
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from opentrons_control.backend.app.settings.config import settings
from opentrons_control.backend.app.security import hash_password

MIN_PASSWORD_LEN = 8
CONNECT_RETRIES = 10
CONNECT_BACKOFF_SECONDS = 1.5


def _engine_when_ready():
    """
    Return an engine once the database accepts connections and the users
    table is queryable, retrying briefly to cover the window where the
    container reports up but Postgres is still finishing initialisation.
    """
    engine = create_engine(settings.database_url)
    last_err: Exception | None = None
    for attempt in range(CONNECT_RETRIES):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1 FROM users LIMIT 1"))
            return engine
        except (OperationalError, ProgrammingError) as exc:
            last_err = exc
            time.sleep(CONNECT_BACKOFF_SECONDS * (attempt + 1))
    print(f"Database not ready after {CONNECT_RETRIES} attempts: {last_err}")
    sys.exit(1)


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
    engine = _engine_when_ready()

    if _admin_exists(engine):
        print("Admin account already exists. Skipping.")
        return

    if not sys.stdin.isatty():
        print("No admin account exists and no terminal is attached to create one.")
        print("Run: docker compose exec backend python -m app.scripts.seed_admin")
        sys.exit(1)

    name, password = _prompt_admin()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (name, role, password_hash) "
                "VALUES (:name, 'admin', :password_hash)"
            ),
            {"name": name, "password_hash": hash_password(password)},
        )
    print(f"Admin account '{name}' created.")


if __name__ == "__main__":
    main()