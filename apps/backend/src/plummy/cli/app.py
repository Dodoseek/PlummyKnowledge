"""``python -m plummy.cli`` commands.

    setup init-token [--rotate]   issue the one-time bootstrap token into BOOTSTRAP_DIR/setup-token
    setup status                  show whether the instance is initialized
    admin create                  create an administrator (initializes an uninitialized instance)
    admin reset-password          set a new password and revoke all sessions of the user

Secrets are never accepted as arguments (they would end up in shell history and ``ps``) and never printed:
passwords are read with a hidden prompt or from stdin (``--password-stdin``), and the bootstrap token is only
written to its file.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any, TextIO

from plummy.application.errors import ApplicationError, InvalidInputError
from plummy.application.setup.use_cases import TokenIssueOutcome
from plummy.application.setup.validation import AdministratorInput
from plummy.composition import CliServices, build_cli_services
from plummy.core.config import ConfigurationError, Settings, get_settings
from plummy.infrastructure.db.errors import DATABASE_UNAVAILABLE_ERRORS

ServicesFactory = Callable[[Settings], CliServices]


class CliError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m plummy.cli", description="PlummyKnowledge server CLI")
    groups = parser.add_subparsers(dest="group", required=True)

    setup = groups.add_parser("setup", help="first-run setup").add_subparsers(dest="command", required=True)
    init_token = setup.add_parser("init-token", help="issue the one-time bootstrap token (idempotent)")
    init_token.add_argument(
        "--rotate", action="store_true", help="replace a valid token (lost or leaked token)"
    )
    setup.add_parser("status", help="show the setup state")

    admin = groups.add_parser("admin", help="administrators").add_subparsers(dest="command", required=True)
    create = admin.add_parser("create", help="create an administrator")
    create.add_argument("--email", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    reset = admin.add_parser("reset-password", help="reset a user's password and revoke their sessions")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    return parser


def _read_password(*, from_stdin: bool, stdin: TextIO) -> str:
    if from_stdin:
        password = stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            raise CliError("passwords do not match")
    if not password:
        raise CliError("password is empty")
    return password


async def _run(args: argparse.Namespace, services: CliServices, out: TextIO, stdin: TextIO) -> None:
    match (args.group, args.command):
        case ("setup", "init-token"):
            result = await services.issue_bootstrap_token.execute(rotate=args.rotate)
            if result.outcome is TokenIssueOutcome.ALREADY_INITIALIZED:
                print("Instance is already initialized; no bootstrap token issued.", file=out)
            elif result.outcome is TokenIssueOutcome.UNCHANGED:
                print(f"A valid bootstrap token already exists: {result.location}", file=out)
            else:
                print(f"Bootstrap token written to {result.location}", file=out)
            if result.expires_at is not None:
                print(f"Expires at {result.expires_at.isoformat()}", file=out)
        case ("setup", "status"):
            status = await services.setup_status.execute()
            print(f"initialized: {'yes' if status.initialized else 'no'}", file=out)
            print(f"web setup: {'enabled' if status.web_setup_enabled else 'disabled'}", file=out)
            if not status.initialized:
                present = await asyncio.to_thread(Path(services.token_location).exists)
                print(
                    f"token file: {services.token_location} ({'present' if present else 'absent'})", file=out
                )
        case ("admin", "create"):
            password = _read_password(from_stdin=args.password_stdin, stdin=stdin)
            created = await services.create_administrator.execute(
                AdministratorInput(email=args.email, display_name=args.display_name, password=password)
            )
            print(f"Administrator created: {created.user_id}", file=out)
            if created.initialized_instance:
                print("Instance initialized; the bootstrap token is no longer valid.", file=out)
        case ("admin", "reset-password"):
            password = _read_password(from_stdin=args.password_stdin, stdin=stdin)
            revoked = await services.reset_password.execute(email=args.email, password=password)
            print(f"Password reset; {revoked} session(s) revoked.", file=out)
        case _:  # pragma: no cover - argparse rejects unknown commands
            raise CliError("unknown command")


async def _main_async(
    args: argparse.Namespace, factory: ServicesFactory, settings: Settings, out: TextIO, stdin: TextIO
) -> None:
    services = factory(settings)
    try:
        await _run(args, services, out, stdin)
    finally:
        await services.aclose()


def main(
    argv: Sequence[str] | None = None,
    *,
    services_factory: ServicesFactory = build_cli_services,
    settings: Settings | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    out, err, inp = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    args = _parser().parse_args(argv)
    try:
        resolved = settings if settings is not None else get_settings()
        coroutine: Coroutine[Any, Any, None] = _main_async(args, services_factory, resolved, out, inp)
        asyncio.run(coroutine)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=err)
        return 1
    except InvalidInputError as exc:
        print(f"error: {exc.field}: {exc}", file=err)
        return 1
    except (ApplicationError, CliError) as exc:
        print(f"error: {exc}", file=err)
        return 1
    except DATABASE_UNAVAILABLE_ERRORS as exc:
        print(f"error: database unavailable ({type(exc).__name__})", file=err)
        return 1
    except OSError as exc:
        print(f"error: {type(exc).__name__}: {exc.strerror or exc}", file=err)
        return 1
    return 0
