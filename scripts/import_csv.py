#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal, init_db, Item
from app.utils import parse_date_human


async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_csv.py <path_to_csv>")
        print("CSV headers: title,expires_at[,chat_id]")
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    await init_db()

    added = 0
    skipped = 0

    async with SessionLocal() as session:
        with csv_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                title = (row.get("title") or "").strip()
                expires_str = (row.get("expires_at") or "").strip()
                chat_id_str = (row.get("chat_id") or "").strip()

                if not title or not expires_str:
                    skipped += 1
                    continue

                dt = parse_date_human(expires_str)
                if not dt:
                    skipped += 1
                    continue

                chat_id = int(chat_id_str) if chat_id_str.isdigit() else None

                item = Item(title=title, expires_at=dt, chat_id=chat_id)
                session.add(item)
                added += 1

        await session.commit()

    print(f"Imported: {added}, skipped: {skipped}")


if __name__ == "__main__":
    asyncio.run(main())