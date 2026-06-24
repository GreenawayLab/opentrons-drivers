from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

SQL_DIR = Path(__file__).parent / "sql"


def load_sql(path: str) -> str:
    full = SQL_DIR / path
    if not full.exists():
        raise FileNotFoundError(f"SQL file not found: {full}")
    return full.read_text()


def fetch(db: Session, path: str, params: dict | None = None) -> list[dict]:
    """Execute a SELECT and return all rows as dicts."""
    result = db.execute(text(load_sql(path)), params or {})
    return [dict(row._mapping) for row in result.fetchall()]


def fetch_one(db: Session, path: str, params: dict | None = None) -> dict | None:
    """Execute a SELECT and return the first row as a dict, or None."""
    result = db.execute(text(load_sql(path)), params or {})
    row = result.fetchone()
    return dict(row._mapping) if row else None


def fetch_scalar(db: Session, path: str, params: dict | None = None) -> Any:
    """Execute a SELECT returning a single value."""
    result = db.execute(text(load_sql(path)), params or {})
    row = result.fetchone()
    return row[0] if row else None


def execute(db: Session, path: str, params: dict | None = None, commit: bool = True) -> None:
    """Execute an INSERT/UPDATE/DELETE. Commits immediately unless commit is False."""
    db.execute(text(load_sql(path)), params or {})
    if commit:
        db.commit()


def execute_returning(
    db: Session, path: str, params: dict | None = None, commit: bool = True
) -> dict | None:
    """Execute an INSERT/UPDATE ... RETURNING. Commits unless commit is False."""
    result = db.execute(text(load_sql(path)), params or {})
    if commit:
        db.commit()
    row = result.fetchone()
    return dict(row._mapping) if row else None