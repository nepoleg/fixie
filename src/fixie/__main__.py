"""Command-line entry point: `python -m fixie` or the installed `fixie` script."""

import argparse
import concurrent.futures
import logging
import sys
from pathlib import Path

from fixie.fake_gateway import FakeGateway
from fixie.helper import HelperConfig, HelperError, refresh_gradle_cache
from fixie.model import Identity
from fixie.orchestrator import Orchestrator
from fixie.settings import SettingsError, load_settings


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fixie")
    parser.add_argument("--once", action="store_true", help="poll and dispatch once, then exit")
    parser.add_argument("--fake-fixture", type=Path, help="run against an in-memory ADO fixture, not real ADO")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="path to config.toml")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="path to the .env secrets file")
    parser.add_argument(
        "--refresh-gradle-cache", type=Path, metavar="MODULES2",
        help="copy a local Gradle modules-2 folder into the shared read-only cache volume, then exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run fixie's orchestrator from the command line; returns the process exit code."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)

    if args.fake_fixture is None and args.refresh_gradle_cache is None:
        print("fixie: real ADO gateway not implemented yet, use --fake-fixture <path>", file=sys.stderr)
        return 2

    try:
        settings = load_settings(args.config, args.env)
    except SettingsError as exc:
        print(f"fixie: {exc}", file=sys.stderr)
        return 2

    if args.refresh_gradle_cache is not None:
        try:
            print(refresh_gradle_cache(HelperConfig.from_settings(settings), args.refresh_gradle_cache.resolve()))
        except (HelperError, ValueError) as exc:
            print(f"fixie: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        gw = FakeGateway(args.fake_fixture, Identity("fixie", settings.owner_email))
    except (OSError, ValueError) as exc:
        print(f"fixie: cannot load fixture {args.fake_fixture}: {exc}", file=sys.stderr)
        return 2

    orch = Orchestrator(gw, settings)

    if args.once:
        try:
            orch.recover()
            concurrent.futures.wait(orch.poll_once())
        finally:
            orch.shutdown()
    else:
        orch.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
