#!/usr/bin/env python3
"""
Admin / user CLI for RamboQuant.

Run on the server with the app's venv activated:

    python -m scripts.manage bootstrap-admin --username ambore \
        --display-name "Ramana Ambore" --email ramboquant@gmail.com --super

    python -m scripts.manage create-user --username rambo --role admin \
        --display-name "Rambo" --email ramboquant@gmail.com

Both commands prompt interactively for the password (no plaintext in
shell history). A super-admin is intended for break-glass / role-grant
operations only — day-to-day admin work goes through `--role admin`.

The script imports the live application stack (database.py, models.py,
auth.py) so it inherits the same hashing / validation / DB selection
that the API uses; nothing is duplicated.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from backend.api.database import async_session, init_db  # noqa: E402
from backend.api.models import User  # noqa: E402
from backend.api.routes.auth import hash_password  # noqa: E402
from backend.shared.helpers.utils import (  # noqa: E402
    validate_email,
    validate_password_standard,
)


def _prompt_password() -> str:
    pw1 = getpass.getpass("Password: ")
    pw2 = getpass.getpass("Confirm:  ")
    if pw1 != pw2:
        sys.exit("✗ Passwords do not match")
    ok, msg = validate_password_standard(pw1)
    if not ok:
        sys.exit(f"✗ {msg}")
    return pw1


async def _bootstrap(args: argparse.Namespace) -> None:
    if not validate_email(args.email):
        sys.exit(f"✗ Invalid email: {args.email!r}")
    password = _prompt_password()

    await init_db()
    async with async_session() as session:
        existing = await session.execute(
            select(User).where(User.username == args.username)
        )
        user = existing.scalar_one_or_none()
        if user and not args.update:
            sys.exit(
                f"✗ User {args.username!r} already exists. "
                f"Pass --update to overwrite the password / role."
            )
        if user:
            user.password_hash    = hash_password(password)
            user.role             = "admin"
            user.is_super         = bool(args.super)
            user.is_active        = True
            user.is_approved      = True
            user.email_verified   = True
            user.email            = args.email
            user.display_name     = args.display_name or user.display_name
            user.terminated_at    = None
            user.suspended_at     = None
            user.token_version    = (user.token_version or 1) + 1
            verb = "updated"
        else:
            user = User(
                username       = args.username,
                password_hash  = hash_password(password),
                role           = "admin",
                display_name   = args.display_name or args.username,
                email          = args.email,
                is_super       = bool(args.super),
                is_active      = True,
                is_approved    = True,
                email_verified = True,
                token_version  = 1,
            )
            session.add(user)
            verb = "created"
        await session.commit()
    print(
        f"✓ {verb} {args.username!r} (role=admin, is_super={bool(args.super)}, "
        f"email={args.email})"
    )


async def _create(args: argparse.Namespace) -> None:
    if args.email and not validate_email(args.email):
        sys.exit(f"✗ Invalid email: {args.email!r}")
    password = _prompt_password()

    await init_db()
    async with async_session() as session:
        existing = await session.execute(
            select(User).where(User.username == args.username)
        )
        if existing.scalar_one_or_none():
            sys.exit(f"✗ User {args.username!r} already exists")
        user = User(
            username       = args.username,
            password_hash  = hash_password(password),
            role           = args.role,
            display_name   = args.display_name or args.username,
            email          = args.email or None,
            is_active      = True,
            is_approved    = bool(args.approve),
            email_verified = bool(args.verify),
            token_version  = 1,
        )
        session.add(user)
        await session.commit()
    print(
        f"✓ created {args.username!r} (role={args.role}, "
        f"approved={bool(args.approve)}, email_verified={bool(args.verify)})"
    )


def main() -> None:
    p = argparse.ArgumentParser(prog="scripts/manage.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    boot = sub.add_parser(
        "bootstrap-admin",
        help="Create or update an admin (optionally super) user. Prompts for password.",
    )
    boot.add_argument("--username",     required=True)
    boot.add_argument("--email",        required=True)
    boot.add_argument("--display-name", default="")
    boot.add_argument("--super",        action="store_true",
                      help="Set is_super=True (highest privilege)")
    boot.add_argument("--update",       action="store_true",
                      help="Overwrite the existing row instead of failing")

    new = sub.add_parser(
        "create-user",
        help="Create a regular user (role=partner by default). Prompts for password.",
    )
    new.add_argument("--username",     required=True)
    new.add_argument("--role",         default="partner",
                     choices=["partner", "admin"])
    new.add_argument("--email",        default="")
    new.add_argument("--display-name", default="")
    new.add_argument("--approve",      action="store_true",
                     help="Mark account as admin-approved (skip approval gate)")
    new.add_argument("--verify",       action="store_true",
                     help="Mark email as verified (skip verification gate)")

    args = p.parse_args()
    if args.cmd == "bootstrap-admin":
        asyncio.run(_bootstrap(args))
    elif args.cmd == "create-user":
        asyncio.run(_create(args))


if __name__ == "__main__":
    main()
