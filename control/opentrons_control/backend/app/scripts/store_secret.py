"""
Store a secret into the encrypted vault.

Reads the secret value from a file or from stdin and stores it under a name,
encrypted with the application key. Runs inside the backend container:

    # from stdin (pipe a key file from the host)
    docker compose exec -T backend python -m app.scripts.store_secret ot3_ssh_key ssh_key < ./ot3_key

    # from a path inside the container
    docker compose exec backend python -m app.scripts.store_secret ot3_ssh_key ssh_key --file /tmp/ot3_key
"""

import argparse
import sys

from app.db.db_session import SessionLocal
from app.vault import put_secret


def main() -> None:
    parser = argparse.ArgumentParser(description="Store an encrypted secret.")
    parser.add_argument("name", help="lookup name, e.g. ot3_ssh_key")
    parser.add_argument("kind", nargs="?", default="other",
                        help="ssh_key | git_deploy | other")
    parser.add_argument("--file", help="read value from this path instead of stdin")
    args = parser.parse_args()

    if args.file:
        with open(args.file, "rb") as f:
            value = f.read()
    else:
        value = sys.stdin.buffer.read()

    if not value:
        print("No secret data provided.")
        sys.exit(1)

    db = SessionLocal()
    try:
        put_secret(db, args.name, value, kind=args.kind)
    finally:
        db.close()

    print(f"Stored '{args.name}' ({len(value)} bytes, kind={args.kind}).")


if __name__ == "__main__":
    main()