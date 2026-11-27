"""`nullwake` command-line interface."""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path


def _alembic_config():
    from alembic.config import Config
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


def cmd_migrate(args) -> int:
    import os

    from alembic import command
    command.upgrade(_alembic_config(), "head")
    print("database is at the latest migration")
    password = os.environ.get("NULLWAKE_APP_DB_PASSWORD", "")
    if password:
        from sqlalchemy import create_engine

        from .config import get_settings
        from .dbroles import ensure_app_role
        role = os.environ.get("NULLWAKE_APP_DB_USER", "nullwake_app")
        engine = create_engine(get_settings().database_url)
        try:
            ensure_app_role(engine, role, password)
        finally:
            engine.dispose()
        print(f"runtime role {role} can read and write rows only (no schema changes, no superuser)")
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .config import get_settings
    from .logging_setup import configure
    configure(get_settings().log_level)
    uvicorn.run("nullwake.app:create_app", factory=True, host=args.host, port=args.port, proxy_headers=False,
                log_config=None, workers=args.workers)
    return 0


def cmd_worker(args) -> int:
    from .config import get_settings
    from .logging_setup import configure
    from .worker import run_forever
    settings = get_settings()
    configure(settings.log_level)
    run_forever(settings, once=args.once)
    return 0


def cmd_worker_health(args) -> int:
    """Container health check: exit 0 only if a worker process on this host reported in recently."""
    import socket

    from sqlalchemy import func, select

    from .config import get_settings
    from .db import session_factory
    from .models import WorkerHeartbeat
    settings = get_settings()
    max_age = args.max_age or settings.stall_seconds + 60
    with session_factory()() as db:
        age = db.scalar(select(func.extract("epoch", func.now() - func.max(WorkerHeartbeat.heartbeat_at)))
                        .where(WorkerHeartbeat.worker_id.like(f"{socket.gethostname()}:%")))
    age = float(age) if age is not None else None
    if age is None or age > max_age:
        print(f"unhealthy: last heartbeat {'never' if age is None else f'{age:.0f}s ago'} (max {max_age}s)")
        return 1
    print(f"healthy: last heartbeat {age:.0f}s ago")
    return 0


def cmd_user(args) -> int:
    from .db import session_scope
    from .security import upsert_user
    if args.delete:
        return _delete_user(args.email)
    if not args.role:
        print("error: --role is required unless --delete is given", file=sys.stderr)
        return 2
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password (14+ characters): ")
        if password != getpass.getpass("Repeat password: "):
            print("passwords differ", file=sys.stderr)
            return 2
    try:
        with session_scope() as db:
            upsert_user(db, args.email, args.role, password, args.name or "")
            from .engine import audit
            audit(db, "cli", "user.upsert", f"user/{args.email.lower()}", role=args.role)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"saved {args.email.lower()} as {args.role}; existing sessions revoked")
    return 0


def _delete_user(email: str) -> int:
    """Erase an account (for example a GDPR or DPDP erasure request). Sessions go with it; the audit log keeps history."""
    from sqlalchemy import select

    from .db import session_scope
    from .engine import audit
    from .models import User
    email = email.strip().lower()
    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"error: no user {email}", file=sys.stderr)
            return 1
        db.delete(user)
        audit(db, "cli", "user.delete", f"user/{email}")
    print(f"deleted {email} and their sessions; API tokens they created are not revoked, check them in Settings")
    return 0


def cmd_apply(args) -> int:
    from sqlalchemy import select
    from .contracts import ContractError, load_bundle
    from .db import session_scope
    from .engine import apply_bundle
    from .models import Connection
    try:
        with session_scope() as db:
            known = dict(db.execute(select(Connection.name, Connection.type)).all())
            bundle = load_bundle(args.paths, known)
            if args.dry_run:
                print(f"valid: {len(bundle.contracts)} contract(s), "
                      f"{sum(len(c.monitors) for c in bundle.contracts)} check(s)")
                db.rollback()
                return 0
            summary = apply_bundle(db, bundle, args.actor, prune=args.prune)
    except (ContractError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for key, items in summary.as_dict().items():
        for item in items:
            print(f"{key:>9}  {item}")
    return 0


def cmd_check(args) -> int:
    """Run contracts directly against the warehouse (no Nullwake server needed). Exit 1 on any failure."""
    from .connectors import ConnectorError, open_connector
    from .contracts import ContractError, load_bundle
    from .monitors import Result, evaluate
    try:
        bundle = load_bundle(args.paths)
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    conns = {c.name: c for c in bundle.connections}
    contracts = [c for c in bundle.contracts if not args.contract or c.name in args.contract]
    results, failed = [], False
    for contract in contracts:
        conn_def = conns.get(contract.connection)
        if conn_def is None:
            print(f"error: contract {contract.name} uses connection '{contract.connection}' which is not in the given files",
                  file=sys.stderr)
            return 2
        try:
            conn = open_connector(conn_def.name, conn_def.type, conn_def.spec, args.timeout)
        except ConnectorError as exc:
            checks = [(m, Result("error", str(exc))) for m in contract.monitors]
        else:
            with conn:
                checks = [(m, evaluate(conn, m.kind, m.dataset, m.params)) for m in contract.monitors]
        for m, r in checks:
            failed |= r.status != "pass"
            results.append({"check": m.name, "kind": m.kind, "severity": m.severity, "status": r.status,
                            "message": r.message, "value": r.value})
    if args.format == "json":
        print(json.dumps({"passed": not failed, "results": results}, indent=2))
    else:
        width = max((len(r["check"]) for r in results), default=10)
        for r in results:
            mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}[r["status"]]
            print(f"{mark}  {r['check']:<{width}}  {r['message']}")
        print(f"\n{sum(r['status'] == 'pass' for r in results)}/{len(results)} checks passed")
    return 1 if failed else 0


def cmd_init(args) -> int:
    """Draft starter contracts from the warehouse's own table definitions."""
    from .connectors import ConnectorError, open_connector
    from .contracts import ContractError, load_bundle
    from .dbt import ManifestError, load_manifest, select
    from .init import run_init
    if not args.schema and not args.table and not args.dbt:
        print("error: give --schema SCHEMA, --table SCHEMA.TABLE and/or --dbt target/manifest.json", file=sys.stderr)
        return 2
    if args.select and not args.dbt:
        print("error: --select only applies with --dbt", file=sys.stderr)
        return 2
    try:
        bundle = load_bundle([args.connection_file])
    except ContractError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    conns = [c for c in bundle.connections if not args.connection or c.name == args.connection]
    if len(conns) != 1:
        names = ", ".join(c.name for c in bundle.connections) or "none"
        print(f"error: expected exactly one Connection in {args.connection_file} (found: {names}); "
              "pick one with --connection NAME", file=sys.stderr)
        return 2
    conn_def = conns[0]
    out_dir = Path(args.out)
    dbt_tables = []
    if args.dbt:
        try:
            dbt_tables = select(load_manifest(Path(args.dbt), three_part=conn_def.type != "postgres", dialect=conn_def.type),
                                args.select or [])
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not dbt_tables:
            print("error: no dbt models, snapshots, seeds or sources matched"
                  + (f" --select {' '.join(args.select)}" if args.select else ""), file=sys.stderr)
            return 2
    try:
        with open_connector(conn_def.name, conn_def.type, conn_def.spec, args.timeout) as conn:
            written, skipped = run_init(conn, conn_def.name, args.schema or [], args.table or [], out_dir,
                                        args.owner, args.force, args.interval, dbt_tables)
    except ConnectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path, draft in written:
        print(f"wrote {path}  ({draft.dataset})")
        for note in draft.notes:
            print(f"      note: {note}")
    for msg in skipped:
        print(f"skipped {msg}")
    if not written:
        return 1
    print(f"\n{len(written)} contract(s) drafted. Review the TODOs, then try them against the warehouse:\n"
          f"  nullwake check {args.connection_file} {out_dir}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nullwake", description="Data reliability that runs inside your network.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=cmd_migrate)
    p = sub.add_parser("serve", help="run the web UI and API")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("worker", help="run the scheduler/worker loop")
    p.add_argument("--once", action="store_true", help="run one scheduling pass and exit")
    p.set_defaults(fn=cmd_worker)
    p = sub.add_parser("worker-health", help="exit 0 if this host's worker reported in recently (container health)")
    p.add_argument("--max-age", type=int, default=0, help="seconds; default: the stall limit + 60")
    p.set_defaults(fn=cmd_worker_health)
    p = sub.add_parser("user", help="create or update a user (resets password, revokes sessions), or --delete one")
    p.add_argument("email")
    p.add_argument("--role", choices=("viewer", "editor", "admin"))
    p.add_argument("--delete", action="store_true", help="delete the user and their sessions")
    p.add_argument("--name")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(fn=cmd_user)
    p = sub.add_parser("apply", help="apply Connection/DataContract/AlertChannel YAML to the server database")
    p.add_argument("paths", nargs="+")
    p.add_argument("--prune", action="store_true", help="delete contracts that are not in the given files")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--actor", default="cli")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("check", help="run contracts directly against the warehouse; exit 1 on failure (CI)")
    p.add_argument("paths", nargs="+")
    p.add_argument("--contract", action="append", help="only run this contract (repeatable)")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("init", help="draft contracts from the warehouse's table definitions")
    p.add_argument("connection_file", help="YAML file with the Connection to read from")
    p.add_argument("--connection", help="connection name, if the file holds more than one")
    p.add_argument("--schema", action="append", help="draft a contract for every table in this schema (repeatable)")
    p.add_argument("--table", action="append", help="draft a contract for this table, e.g. analytics.orders (repeatable)")
    p.add_argument("--dbt", metavar="MANIFEST", help="draft from a dbt manifest (target/manifest.json): its tests, "
                   "source freshness and owners become checks")
    p.add_argument("--select", action="append", help="with --dbt: a model or source table name, tag:NAME or "
                   "source:NAME (repeatable)")
    p.add_argument("--out", default="contracts", help="directory to write contracts to (default: contracts)")
    p.add_argument("--owner", default="data-team@example.com", help="owner written into each contract")
    p.add_argument("--interval", type=int, default=60, help="check interval in minutes (default: 60)")
    p.add_argument("--force", action="store_true", help="overwrite existing contract files")
    p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(fn=cmd_init)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
