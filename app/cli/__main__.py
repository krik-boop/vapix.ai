"""CLI: python -m app.cli {amo|hh} sync {full|incremental}"""

import argparse
import logging
import sys

from app.core.config import ConfigError, get_settings
from app.core.db import get_sessionmaker
from app.core.logging import setup_logging
from app.integrations.sync_state import MODES, SyncReport

logger = logging.getLogger("app.cli")


def _amo_sync(mode: str) -> SyncReport:
    from app.integrations.amo.client import AmoClient
    from app.integrations.amo.sync import AmoSync

    settings = get_settings()
    with get_sessionmaker()() as session, AmoClient(settings) as client:
        return AmoSync(session, client, settings).run(mode)


def _hh_sync(mode: str) -> SyncReport:
    from app.integrations.hh.client import HhClient
    from app.integrations.hh.sync import HhSync

    settings = get_settings()
    with get_sessionmaker()() as session, HhClient(settings, session=session) as client:
        return HhSync(session, client, settings).run(mode)


COMMANDS = {"amo": _amo_sync, "hh": _hh_sync}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-business-os", description="Read-only data sync")
    sources = parser.add_subparsers(dest="source", required=True)
    for source in COMMANDS:
        src = sources.add_parser(source, help=f"{source} commands")
        actions = src.add_subparsers(dest="action", required=True)
        sync = actions.add_parser("sync", help=f"sync {source} data into the database")
        sync.add_argument("mode", choices=MODES)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = get_settings()
    except Exception as exc:  # pydantic validation error; values are not echoed
        print(f"Invalid configuration: {type(exc).__name__}", file=sys.stderr)
        return 2
    setup_logging(settings)
    try:
        report = COMMANDS[args.source](args.mode)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    except Exception:
        logger.exception("%s sync %s failed", args.source, args.mode)
        return 1
    logger.info("%s sync %s done: counts=%s skipped=%s", args.source, args.mode, report.counts, report.skipped)
    if not report.ok:
        logger.error("%s sync %s finished with errors: %s", args.source, args.mode, report.errors)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
