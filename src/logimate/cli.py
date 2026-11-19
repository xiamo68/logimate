import argparse
import json
import os
from pathlib import Path
from time import sleep

from . import store
from .evolution import run_one
from .seed import DATA_VERSION, export_cases, fingerprint, snapshot
from .workflow import DeepSeek


def main():
    parser = argparse.ArgumentParser(prog="logimate")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--datasets", type=Path, default=Path(".logimate/datasets"))
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
        elif args.command == "worker":
            mode = os.getenv("LOGIMATE_MODE", "demo")
            if mode not in ("demo", "deepseek"):
                parser.error("LOGIMATE_MODE must be demo or deepseek")
            model = DeepSeek(os.getenv("DEEPSEEK_MODEL"), os.getenv("DEEPSEEK_API_KEY")) if mode == "deepseek" else None
            while True:
                completed = run_one(engine, args.datasets, model)
                if args.once:
                    break
                if not completed:
                    sleep(0.5)
        else:
            data = snapshot()
            digest = fingerprint(data)
            inserted = store.seed_database(engine, data, digest)
            export_cases(data, args.output)
            print(json.dumps({"data_version": args.data_version, "sha256": digest, "inserted": inserted,
                              "orders": len(data["business"])}))
    finally:
        engine.dispose()
