"""
One-off migration: set email_verified=True for all active users who have
a non-empty email but whose email_verified flag is still False.

Logic matches what the DB default gave them at column-add time — the column
was added with DEFAULT FALSE, so every user who registered before the column
existed landed with email_verified=False even though they're active and
operational (and already passing emails to alert channels).

Run on dev first, verify the row count, then run on prod:

  # dev  (from the dev checkout with deploy_branch != 'main')
  cd /opt/ramboq_dev && ./venv/bin/python scripts/fix_email_verified.py

  # prod  (from the prod checkout with deploy_branch == 'main')
  cd /opt/ramboq && ./venv/bin/python scripts/fix_email_verified.py

The script reads the DB URL from the same path as the API
(secrets.yaml + backend_config.yaml → deploy_branch → db_name).
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select, update

from backend.api.database import async_session
from backend.api.models import User
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


async def fix_email_verified() -> int:
    """
    Count rows affected before running, apply the UPDATE, then print results.

    Returns the number of rows updated.
    """
    async with async_session() as session:
        # --- Pre-flight: count rows that will be touched ---
        count_q = select(func.count()).where(
            User.is_active == True,           # noqa: E712
            User.email.isnot(None),
            User.email != "",
            User.email_verified == False,      # noqa: E712
        )
        affected_count: int = (await session.execute(count_q)).scalar_one()

        if affected_count == 0:
            logger.info(
                "fix_email_verified: no rows need updating — "
                "all active users with emails already have email_verified=True"
            )
            return 0

        logger.info(
            "fix_email_verified: found %d active user(s) with email but "
            "email_verified=False — updating now",
            affected_count,
        )

        # --- Sample preview (up to 10 rows) ---
        sample_q = (
            select(User.username, User.email, User.role)
            .where(
                User.is_active == True,        # noqa: E712
                User.email.isnot(None),
                User.email != "",
                User.email_verified == False,  # noqa: E712
            )
            .limit(10)
        )
        sample = (await session.execute(sample_q)).all()
        for row in sample:
            logger.info(
                "  will verify: username=%r  email=%r  role=%r",
                row.username, row.email, row.role,
            )
        if affected_count > 10:
            logger.info("  … and %d more", affected_count - 10)

        # --- Apply the UPDATE ---
        stmt = (
            update(User)
            .where(
                User.is_active == True,        # noqa: E712
                User.email.isnot(None),
                User.email != "",
                User.email_verified == False,  # noqa: E712
            )
            .values(email_verified=True)
        )
        result = await session.execute(stmt)
        await session.commit()

        rows_updated: int = result.rowcount
        logger.info(
            "fix_email_verified: committed — %d row(s) updated",
            rows_updated,
        )
        return rows_updated


if __name__ == "__main__":
    updated = asyncio.run(fix_email_verified())
    print(f"fix_email_verified: {updated} row(s) updated")
