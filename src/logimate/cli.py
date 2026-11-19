import argparse
import json
import os
from pathlib import Path

from . import store
from .seed import DATA_VERSION, export_cases, fingerprint, snapshot


def main():
    parser = argparse.ArgumentParser(prog="logimate")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    demo = commands.add_parser("demo")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    seed = demo_commands.add_parser("seed")
    seed.add_argument("--data-version", choices=[DATA_VERSION], default=DATA_VERSION)
    seed.add_argument("--output", type=Path, default=Path(".logimate/datasets"))
    args = parser.parse_args()
    engine = store.database(os.environ["MYSQL_DSN"])
    try:
        if args.command == "migrate":
            store.migrate(engine)
        else:
            data = snapshot()
            digest = fingerprint(data)
            inserted = store.seed_database(engine, data, digest)
            export_cases(data, args.output)
            print(json.dumps({"data_version": args.data_version, "sha256": digest, "inserted": inserted,
                              "orders": len(data["business"])}))
    finally:
        engine.dispose()
