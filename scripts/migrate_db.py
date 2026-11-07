"""Initialize / migrate the SQLite database schema and verify all tables."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import load_config
from src.core.database import Database


async def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or migrate bot SQLite database")
    parser.add_argument("--db", default=None, help="Override database file path")
    args = parser.parse_args()

    config = load_config()
    db = Database(config=config.database, db_path=args.db)
    await db.connect()

    cursor = await db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [row[0] for row in await cursor.fetchall()]
    print(f"Database initialized at: {args.db or config.database.path}")
    print(f"Tables verified ({len(tables)}): {', '.join(tables)}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())

