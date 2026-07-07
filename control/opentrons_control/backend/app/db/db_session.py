from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from opentrons_control.backend.app.settings.config import settings
import time

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

def wait_for_db(target_engine: Engine | None = None, retries: int = 30, interval: float = 1.0) -> Engine:
    """Block until the database accepts a real connection, then return the engine.

    On a fresh volume, postgres reports healthy while initdb applies the schema
    over an internal socket, so a backend connecting over TCP a beat too early
    gets OperationalError and, without a retry, dies. The postgres entrypoint
    only opens the TCP listener AFTER initdb finishes, so a successful SELECT 1
    also means the schema is applied — callers may query real tables right after.

    :param target_engine: Engine to probe; defaults to this module's engine.
    :param retries: Maximum attempts before giving up.
    :param interval: Seconds between attempts.
    :returns: The ready engine, so a script can write ``engine = wait_for_db(...)``.
    :raises RuntimeError: If the database is unreachable after all retries.
    """
    eng = target_engine or engine
    last: Exception | None = None
    for _ in range(retries):
        try:
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            return eng
        except OperationalError as exc:
            last = exc
            time.sleep(interval)
    raise RuntimeError(f"database not reachable after {retries} attempts: {last}")

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()