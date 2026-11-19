import argparse
import json
import os
from pathlib import Path
from time import sleep

from . import store
from .benchmark_data import DEFAULT_PATH, freeze
from .evaluation import evaluate
from .evolution import run_one
from .memory import index_preferences
from .seed import DATA_VERSION, export_cases, fingerprint, snapshot
from .retrieval import Vectors, index_policies
from .workflow import DeepSeek


def main():
    parser = argparse.ArgumentParser(prog="logimate")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    index = commands.add_parser("index-policies")
    index.add_argument("--data-version", default=DATA_VERSION)
    commands.add_parser("index-preferences")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--datasets", type=Path, default=Path(".logimate/datasets"))
    demo = commands.add_parser("demo")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    seed = demo_commands.add_parser("seed")
    seed.add_argument("--data-version", choices=[DATA_VERSION], default=DATA_VERSION)
    seed.add_argument("--output", type=Path, default=Path(".logimate/datasets"))
    eval_commands = commands.add_parser("eval").add_subparsers(dest="kind", required=True)
    freezer = eval_commands.add_parser("freeze")
    freezer.add_argument("--output", type=Path, default=DEFAULT_PATH)
    for name in ("logistics", "rag", "grounding"):
        task = eval_commands.add_parser(name)
        task.add_argument("--mode", choices=("demo", "deepseek"), default="demo")
        task.add_argument("--frozen", type=Path, default=DEFAULT_PATH)
        task.add_argument("--output", type=Path, default=Path(".logimate/evaluations"))
        if name == "logistics":
            task.add_argument("--tenant", default="alpha")
            task.add_argument("--fixed-version", required=True)
            task.add_argument("--evolved-version", required=True)
        else:
            task.add_argument("--max-requests", type=int)
    args = parser.parse_args()
    if args.command == "eval" and args.kind == "freeze":
        frozen = freeze(args.output)
        print(json.dumps({"benchmark_revision": frozen["benchmark_revision"], "sha256": frozen["sha256"],
                          "logistics": len(frozen["logistics"]), "aftersales": len(frozen["aftersales"])}))
        return
    engine = store.database(os.environ["MYSQL_DSN"])
    try:
        if args.command == "migrate":
            store.migrate(engine)
        elif args.command in ("index-policies", "index-preferences"):
            vectors = Vectors.from_environment()
            try:
                if args.command == "index-policies":
                    print(json.dumps({"policy_chunks": index_policies(engine, vectors, args.data_version)}))
                else:
                    print(json.dumps({"preferences_indexed": index_preferences(engine, vectors)}))
            finally:
                vectors.close()
        elif args.command == "eval":
            model = None
            if args.mode == "deepseek":
                limit = 0 if args.kind == "logistics" else args.max_requests
                required = {"logistics": 0, "rag": 60, "grounding": 120}[args.kind]
                if limit is None or limit < required:
                    parser.error(f"DeepSeek {args.kind} requires --max-requests of at least {required}")
                model = DeepSeek(os.getenv("DEEPSEEK_MODEL"), os.getenv("DEEPSEEK_API_KEY"), request_limit=limit)
            report = evaluate(engine, args, model)
            print(json.dumps(report, ensure_ascii=True))
        elif args.command == "worker":
            mode = os.getenv("LOGIMATE_MODE", "demo")
            if mode not in ("demo", "deepseek"):
                parser.error("LOGIMATE_MODE must be demo or deepseek")
            limit = os.getenv("LOGIMATE_DEEPSEEK_MAX_REQUESTS")
            model = DeepSeek(os.getenv("DEEPSEEK_MODEL"), os.getenv("DEEPSEEK_API_KEY"),
                             request_limit=int(limit) if limit else None) if mode == "deepseek" else None
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
