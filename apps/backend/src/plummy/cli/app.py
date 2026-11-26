"""``python -m plummy.cli`` commands.

    setup init-token [--rotate]   issue the one-time bootstrap token into BOOTSTRAP_DIR/setup-token
    setup status                  show whether the instance is initialized
    admin create                  create an administrator (initializes an uninitialized instance)
    admin reset-password          set a new password and revoke all sessions of the user
    worker check                  send a diagnostic task to the Celery worker and wait for its result
    secrets init-keyring          create SECRETS_KEYRING_FILE with one new key if it does not exist
    secrets generate-key          print a new key ring entry (the only command that prints a key)
    secrets status                show the key ring and how many secrets use each key
    secrets rotate                re-encrypt all secrets with the primary key (safe to repeat)
    eval run                      retrieval evaluation of a dataset through upload, workers and search
    maintenance export-deletions  on the source database: deletions since a backup, to a new file
    maintenance reapply-deletions on a restored instance: delete again what the file lists (idempotent)
    maintenance export-objects    every stored object as a tar archive (file or stdout)
    maintenance import-objects    store the objects of such an archive (existing ones are kept)
    maintenance prune-audit       delete audit records older than AUDIT_RETENTION_DAYS (schema owner role)
    db grant-runtime              grant the runtime role of the services exactly its privileges (ADR-024)

Secrets are never accepted as arguments (they would end up in shell history and ``ps``) and never printed:
passwords are read with a hidden prompt or from stdin (``--password-stdin``), and the bootstrap token is only
written to its file. Key material is printed only by ``secrets generate-key``, run on the server.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any, Protocol, TextIO
from uuid import UUID

from sqlalchemy.exc import DBAPIError

from plummy.application.errors import ApplicationError, InvalidInputError
from plummy.application.evaluation.dataset import DatasetError, parse_dataset
from plummy.application.evaluation.runner import DEFAULT_KS, EvalOptions, operator_context
from plummy.application.maintenance.audit_retention import PruneAuditLog
from plummy.application.maintenance.deletions import DeletionExport, DeletionsFileError
from plummy.application.retrieval.model import (
    DEFAULT_KEYWORD_WEIGHT,
    DEFAULT_RRF_K,
    DEFAULT_VECTOR_WEIGHT,
    MAX_BRANCH_WEIGHT,
    MAX_TOP_K,
    SearchMode,
)
from plummy.application.setup.use_cases import TokenIssueOutcome
from plummy.application.setup.validation import AdministratorInput
from plummy.composition import (
    CliServices,
    EvaluationServices,
    RestoreServices,
    RestoreTask,
    build_cli_services,
    build_evaluation_services,
    build_restore_services,
)
from plummy.core.config import ConfigurationError, Settings, get_settings
from plummy.domain.secrets import is_valid_key_id
from plummy.infrastructure.db.engine import create_engine
from plummy.infrastructure.db.errors import DATABASE_UNAVAILABLE_ERRORS
from plummy.infrastructure.db.roles import RuntimeRoleError, grant_runtime
from plummy.infrastructure.security.secret_cipher import (
    KeyRingError,
    create_keyring_file,
    default_key_id,
    generate_key,
    load_keyring,
)
from plummy.workers.app import close_celery_app, create_celery_app
from plummy.workers.tasks.system import MAX_SIMULATED_FAILURES, PING_TASK

ServicesFactory = Callable[[Settings], CliServices]
EvaluationFactory = Callable[[Settings], EvaluationServices]


class MaintenanceFactory(Protocol):
    def __call__(self, settings: Settings, *, task: RestoreTask) -> RestoreServices: ...


CeleryAppFactory = Callable[[Settings], Any]
INSUFFICIENT_PRIVILEGE = "42501"


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

    worker = groups.add_parser("worker", help="Celery worker").add_subparsers(dest="command", required=True)
    check = worker.add_parser("check", help="send a diagnostic task and wait for the result")
    check.add_argument("--timeout", type=_positive_seconds, default=30.0, help="seconds to wait (default 30)")
    check.add_argument(
        "--simulate-failures",
        type=int,
        choices=range(MAX_SIMULATED_FAILURES + 1),
        default=0,
        metavar=f"0..{MAX_SIMULATED_FAILURES}",
        help="fail the first N attempts with a transient error to exercise retries",
    )

    secrets_group = groups.add_parser("secrets", help="encryption keys of stored secrets (ADR-016)")
    secrets_commands = secrets_group.add_subparsers(dest="command", required=True)
    init_keyring = secrets_commands.add_parser(
        "init-keyring", help="create SECRETS_KEYRING_FILE with one new key (never overwrites)"
    )
    init_keyring.add_argument("--key-id", type=_key_id, help="id of the first key (default: today's date)")
    generate = secrets_commands.add_parser("generate-key", help="print a new key ring entry")
    generate.add_argument("--key-id", type=_key_id, help="id of the new key (default: today's date)")
    secrets_commands.add_parser("status", help="key ring and number of secrets per key")
    secrets_commands.add_parser("rotate", help="re-encrypt secrets that are not on the primary key")
    maintenance = groups.add_parser("maintenance", help="backup and restore (ADR-023 §6)")
    maintenance_commands = maintenance.add_subparsers(dest="command", required=True)
    export = maintenance_commands.add_parser(
        "export-deletions", help="on the source database: deletions since the dump, for a restored instance"
    )
    export.add_argument(
        "--since",
        required=True,
        type=_aware_time,
        help="the dump's start time (manifest), ISO 8601 with offset",
    )
    export.add_argument("--output", required=True, type=Path, help="new file (mode 0600) or - for stdout")
    reapply = maintenance_commands.add_parser(
        "reapply-deletions", help="on the restored instance, after migrate: delete again what was deleted"
    )
    reapply.add_argument(
        "--input", required=True, type=Path, help="the file written by export-deletions, or - for stdin"
    )
    export_objects = maintenance_commands.add_parser(
        "export-objects", help="every stored object (documents, quarantine) as a tar archive"
    )
    export_objects.add_argument(
        "--output", required=True, type=Path, help="new file (mode 0600) or - for stdout"
    )
    import_objects = maintenance_commands.add_parser(
        "import-objects", help="store the objects of an export-objects archive (existing ones are kept)"
    )
    import_objects.add_argument("--input", required=True, type=Path, help="the archive, or - for stdin")
    prune_audit = maintenance_commands.add_parser(
        "prune-audit",
        help="delete audit records older than AUDIT_RETENTION_DAYS (migrate container, ADR-024 §6)",
    )
    prune_audit.add_argument("--dry-run", action="store_true", help="only count the records")
    database = groups.add_parser("db", help="database roles (ADR-024)")
    database_commands = database.add_subparsers(dest="command", required=True)
    database_commands.add_parser(
        "grant-runtime", help="reset the privileges of DATABASE_RUNTIME_ROLE; run by migrate after upgrades"
    )
    evaluation = groups.add_parser("eval", help="retrieval evaluation (docs/evaluation/)")
    eval_commands = evaluation.add_subparsers(dest="command", required=True)
    run = eval_commands.add_parser(
        "run", help="index a dataset in its own knowledge base and measure retrieval (JSON report)"
    )
    run.add_argument("--dataset", type=Path, required=True, help="manifest.json and documents/ inside")
    run.add_argument("--email", required=True, help="active user with knowledge_bases.manage to act as")
    run.add_argument("--embedding-profile", type=UUID, required=True, help="active embedding profile id")
    run.add_argument(
        "--mode",
        action="append",
        choices=[mode.value for mode in SearchMode],
        help="search mode to measure (repeatable; default: all three)",
    )
    run.add_argument("--k", type=_ks, default=DEFAULT_KS, help="cut-offs, e.g. 1,3,5,10 (default)")
    run.add_argument("--rrf-k", type=_rrf_k, default=DEFAULT_RRF_K, help=f"RRF k (default {DEFAULT_RRF_K})")
    run.add_argument(
        "--vector-weight",
        type=_weight,
        default=DEFAULT_VECTOR_WEIGHT,
        help=f"weight of the vector branch in hybrid fusion (default {DEFAULT_VECTOR_WEIGHT})",
    )
    run.add_argument(
        "--keyword-weight",
        type=_weight,
        default=DEFAULT_KEYWORD_WEIGHT,
        help=f"weight of the keyword branch in hybrid fusion (default {DEFAULT_KEYWORD_WEIGHT})",
    )
    run.add_argument("--output", type=Path, help="write the report here instead of stdout")
    run.add_argument(
        "--timeout", type=_positive_seconds, default=1800.0, help="indexing wait, seconds (default 1800)"
    )
    return parser


def _ks(value: str) -> tuple[int, ...]:
    try:
        ks = tuple(sorted({int(part) for part in value.split(",") if part.strip()}))
    except ValueError:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from None
    if not ks or ks[0] < 1 or ks[-1] > MAX_TOP_K:
        raise argparse.ArgumentTypeError(f"cut-offs must be between 1 and {MAX_TOP_K}")
    return ks


def _rrf_k(value: str) -> int:
    try:
        k = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer") from None
    if not 1 <= k <= 1000:
        raise argparse.ArgumentTypeError("RRF k must be between 1 and 1000")
    return k


def _weight(value: str) -> float:
    try:
        weight = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a number") from None
    if not 0 <= weight <= MAX_BRANCH_WEIGHT:  # also rejects nan
        raise argparse.ArgumentTypeError(f"weights must be between 0 and {MAX_BRANCH_WEIGHT:g}")
    return weight


def _read_dataset(directory: Path) -> tuple[bytes, dict[str, bytes]]:
    documents = directory / "documents"
    files = {path.name: path.read_bytes() for path in sorted(documents.iterdir()) if path.is_file()}
    return (directory / "manifest.json").read_bytes(), files


async def _eval_run(
    args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO, factory: EvaluationFactory
) -> None:
    manifest, files = await asyncio.to_thread(_read_dataset, args.dataset)
    try:
        dataset = parse_dataset(manifest, files)
    except DatasetError as exc:
        raise CliError(f"dataset: {exc}") from None
    modes = tuple(SearchMode(mode) for mode in dict.fromkeys(args.mode or [m.value for m in SearchMode]))
    options = EvalOptions(
        embedding_profile_id=args.embedding_profile,
        modes=modes,
        ks=args.k,
        indexing_timeout_seconds=args.timeout,
        rrf_k=args.rrf_k,
        vector_weight=args.vector_weight,
        keyword_weight=args.keyword_weight,
    )
    try:
        options.settings()
    except ValueError as exc:
        raise CliError(f"settings: {exc}") from None
    services = factory(settings)
    try:
        context = await operator_context(services.uow, services.clock, args.email)
        report = await services.run.execute(
            context, dataset, options, progress=lambda line: print(line, file=err, flush=True)
        )
    finally:
        await services.aclose()
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        out.write(text)
    else:
        await asyncio.to_thread(args.output.write_text, text, encoding="utf-8")
        print(f"report written to {args.output}", file=err)


def _aware_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "expected an ISO 8601 time, e.g. 2026-11-26T03:00:00+00:00"
        ) from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("the time needs a UTC offset, e.g. 2026-11-26T03:00:00+00:00")
    return parsed


def _write_new_file(path: Path, content: str) -> None:
    """Create ``path`` with mode 0600; an existing file is never overwritten."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)


def _print_counts(title: str, counts: Mapping[str, int], out: TextIO) -> None:
    print(f"{title}: {sum(counts.values())}", file=out)
    for action in sorted(counts):
        print(f"  {action}: {counts[action]}", file=out)


def _print_review(not_reapplied: Mapping[str, int], out: TextIO) -> None:
    if not_reapplied:
        _print_counts(
            "WARNING: changes after the dump that are not reapplied (review them in the source audit log)",
            not_reapplied,
            out,
        )


def _binary_sink(path: Path, out: TextIO) -> contextlib.AbstractContextManager[IO[bytes]]:
    """``-`` is standard output (binary); a path is a new file with mode 0600, never overwritten."""
    if str(path) == "-":
        return contextlib.nullcontext(out.buffer)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def _binary_source(path: Path, stdin: TextIO) -> contextlib.AbstractContextManager[IO[bytes]]:
    if str(path) == "-":
        return contextlib.nullcontext(stdin.buffer)
    return path.open("rb")


def _built[T](value: T | None) -> T:
    if value is None:  # pragma: no cover - the factory builds what the task needs
        raise CliError("this maintenance task is not available")
    return value


async def _maintenance(
    args: argparse.Namespace,
    settings: Settings,
    out: TextIO,
    err: TextIO,
    stdin: TextIO,
    factory: MaintenanceFactory,
) -> None:
    """Reports go to stderr, so ``--output -`` / ``--input -`` can carry the data through a pipe (the backup
    scripts run these commands in the containers, whose filesystems are read-only)."""
    export: DeletionExport | None = None
    if args.command == "reapply-deletions":
        # Read and validate the file before connecting to anything.
        try:
            if str(args.input) == "-":
                text = await asyncio.to_thread(stdin.read)
            else:
                text = await asyncio.to_thread(args.input.read_text, encoding="utf-8")
            export = DeletionExport.from_json(text)
        except DeletionsFileError as exc:
            raise CliError(f"{args.input}: {exc}") from None
    elif (
        args.command in ("export-deletions", "export-objects")
        and str(args.output) != "-"
        and await asyncio.to_thread(args.output.exists)
    ):
        raise CliError(f"{args.output} exists; choose a new file")
    task = {
        "prune-audit": RestoreTask.PRUNE_AUDIT,
        "export-deletions": RestoreTask.EXPORT_DELETIONS,
        "reapply-deletions": RestoreTask.REAPPLY_DELETIONS,
        "export-objects": RestoreTask.OBJECTS,
        "import-objects": RestoreTask.OBJECTS,
    }[args.command]
    services = factory(settings, task=task)
    try:
        match args.command:
            case "prune-audit":
                await _prune_audit(args, settings, _built(services.prune_audit), err)
            case "reapply-deletions":
                export = _built(export)
                report = await _built(services.reapply_deletions).execute(export)
                print(f"Deletions since {export.since.isoformat()}:", file=err)
                _print_counts("applied", report.applied, err)
                _print_counts("already deleted or absent", report.skipped, err)
                if report.refused:
                    _print_counts(
                        "refused (the last active administrator is never deleted)", report.refused, err
                    )
                _print_review(export.not_reapplied, err)
            case "export-deletions":
                exported = await _built(services.export_deletions).execute(args.since)
                if str(args.output) == "-":
                    print(exported.to_json(), file=out)
                else:
                    await asyncio.to_thread(_write_new_file, args.output, exported.to_json())
                _print_counts(
                    f"Deletions since {exported.since.isoformat()} written to {args.output}",
                    Counter(item.action for item in exported.deletions),
                    err,
                )
                _print_review(exported.not_reapplied, err)
            case "export-objects":
                with _binary_sink(args.output, out) as sink:
                    objects = await _built(services.export_objects).execute(sink)
                _print_counts("Objects exported", objects.stored, err)
            case "import-objects":
                with _binary_source(args.input, stdin) as source:
                    objects = await _built(services.import_objects).execute(source)
                _print_counts("Objects stored", objects.stored, err)
                _print_counts("already present, kept", objects.kept, err)
                if objects.rejected:
                    raise CliError(
                        f"{objects.rejected} archive entries are not stored objects and were skipped"
                    )
    finally:
        await services.aclose()


async def _prune_audit(
    args: argparse.Namespace, settings: Settings, use_case: PruneAuditLog, err: TextIO
) -> None:
    retention = timedelta(days=settings.audit_retention_days)
    try:
        report = await use_case.execute(retention, dry_run=args.dry_run)
    except DBAPIError as exc:
        if _sqlstate(exc) != INSUFFICIENT_PRIVILEGE:
            raise
        raise CliError(
            "this database role may not delete audit records: run the command in the migrate container, "
            "which connects as the schema owner (ADR-024 §6)"
        ) from None
    verb = "would be deleted" if report.dry_run else "deleted"
    print(
        f"Audit records older than {report.cutoff.isoformat()} "
        f"({settings.audit_retention_days} days): {report.records} {verb}",
        file=err,
    )


def _sqlstate(exc: DBAPIError) -> str | None:
    original = exc.orig
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return code if isinstance(code, str) else None


async def _grant_runtime(settings: Settings, out: TextIO) -> None:
    role = settings.database_runtime_role
    if role is None:
        raise CliError("DATABASE_RUNTIME_ROLE is not set (the runtime role of the services, ADR-024)")
    engine = create_engine(settings, application_name="plummy-migrate")
    try:
        report = await grant_runtime(engine, role)
    except RuntimeRoleError as exc:
        raise CliError(str(exc)) from None
    finally:
        await engine.dispose()
    print(
        f"Privileges of {report.role}: data access on {report.tables} tables, "
        f"restricted on {', '.join(report.restricted) or 'none'}",
        file=out,
    )


def _key_id(value: str) -> str:
    if not is_valid_key_id(value):
        raise argparse.ArgumentTypeError("must be 1-32 characters of [A-Za-z0-9._-]")
    return value


def _init_keyring(args: argparse.Namespace, settings: Settings, out: TextIO) -> None:
    path = settings.secrets_keyring_file
    if path is None:
        raise CliError("SECRETS_KEYRING_FILE is not set")
    key_id = args.key_id or default_key_id()
    try:
        created = create_keyring_file(path, key_id)
        keyring = load_keyring(path)
    except KeyRingError as exc:
        raise CliError(f"key ring: {exc}") from None
    if created:
        print(f"Key ring created: {path} (primary key {keyring.primary}, mode 0400)", file=out)
        print("Back it up separately from the database: without it stored secrets are lost.", file=out)
    else:
        print(
            f"Key ring already exists: {path} (primary key {keyring.primary}, {len(keyring.keys)} key(s))",
            file=out,
        )


def _generate_key(args: argparse.Namespace, out: TextIO, err: TextIO) -> None:
    key_id = args.key_id or default_key_id()
    print(json.dumps({key_id: generate_key()}), file=out)
    print(
        f'Add this entry to "keys" in the key ring, set "primary" to "{key_id}", restart the API and worker, '
        "then run `secrets rotate`.",
        file=err,
    )


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not 0 < seconds <= 600:
        raise argparse.ArgumentTypeError("must be between 0 and 600 seconds")
    return seconds


def _worker_check(
    args: argparse.Namespace, settings: Settings, out: TextIO, factory: CeleryAppFactory
) -> None:
    from celery.exceptions import TimeoutError as ResultTimeoutError
    from kombu.exceptions import OperationalError
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    app = factory(settings)
    started = time.monotonic()
    try:
        result = app.send_task(PING_TASK, kwargs={"simulate_failures": args.simulate_failures})
        print(f"task {result.id} sent", file=out)
        try:
            value = result.get(timeout=args.timeout, propagate=False)
        except ResultTimeoutError as exc:
            raise CliError(
                f"no result within {args.timeout:g}s (state {result.state}); is the worker running?"
            ) from exc
        elapsed = time.monotonic() - started
        if result.state != "SUCCESS":
            raise CliError(f"task {result.state.lower()} ({type(value).__name__})")
        attempts = value.get("attempts") if isinstance(value, dict) else None
        print(f"state: {result.state}; attempts: {attempts}; elapsed: {elapsed:.2f}s", file=out)
        result.forget()
    except (OperationalError, RedisConnectionError, RedisTimeoutError) as exc:
        raise CliError(f"broker unavailable ({type(exc).__name__})") from exc
    finally:
        close_celery_app(app)


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
        case ("secrets", "status"):
            secrets = await services.secrets_status.execute()
            if not secrets.configured:
                print("key ring: not configured (SECRETS_KEYRING_FILE is not set)", file=out)
            else:
                print(f"key ring: {len(secrets.key_ids)} key(s), primary {secrets.primary_key_id}", file=out)
            print(f"secrets: {secrets.total}", file=out)
            for key_id in sorted(secrets.records_by_key):
                note = " (primary)" if key_id == secrets.primary_key_id else ""
                if key_id in secrets.unknown_key_ids:
                    note = " (MISSING from the key ring: cannot be decrypted)"
                print(f"  {key_id}: {secrets.records_by_key[key_id]}{note}", file=out)
            print(f"on old keys: {secrets.on_old_keys}", file=out)
            if secrets.unknown_key_ids:
                raise CliError("some secrets use keys that are missing from the key ring")
        case ("secrets", "rotate"):
            if services.rotate_secrets is None:
                raise CliError("SECRETS_KEYRING_FILE is not set")
            report = await services.rotate_secrets.execute()
            print(
                f"Rotated {report.rotated} secret(s) to key {report.primary_key_id}; {report.failed} failed.",
                file=out,
            )
            if report.failed:
                raise CliError(
                    f"{report.failed} secret(s) could not be decrypted; see the logs for their ids"
                )
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
    evaluation_factory: EvaluationFactory = build_evaluation_services,
    maintenance_factory: MaintenanceFactory = build_restore_services,
    celery_app_factory: CeleryAppFactory = create_celery_app,
    settings: Settings | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    out, err, inp = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    args = _parser().parse_args(argv)
    try:
        if (args.group, args.command) == ("secrets", "generate-key"):
            _generate_key(args, out, err)
            return 0
        resolved = settings if settings is not None else get_settings()
        if (args.group, args.command) == ("secrets", "init-keyring"):
            _init_keyring(args, resolved, out)
            return 0
        if args.group == "worker":
            _worker_check(args, resolved, out, celery_app_factory)
            return 0
        if args.group == "db":
            asyncio.run(_grant_runtime(resolved, out))
            return 0
        if args.group == "maintenance":
            asyncio.run(_maintenance(args, resolved, out, err, inp, maintenance_factory))
            return 0
        if args.group == "eval":
            asyncio.run(_eval_run(args, resolved, out, err, evaluation_factory))
            return 0
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
