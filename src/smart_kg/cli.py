from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data_loader import load_base_cost_rules, load_demo_bundle, load_rules, write_json
from .excel_loader import standardize_excel_rules
from .paths import REPORT_DIR
from .rule_engine import RuleEngine


def run_demo(args: argparse.Namespace) -> None:
    bundle = load_demo_bundle()
    report = RuleEngine(**bundle).evaluate()
    out_path = Path(args.out) if args.out else REPORT_DIR / "demo_result.json"
    write_json(out_path, report.model_dump(mode="json"))
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print(f"\nReport written to: {out_path}")


def run_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("smart_kg.api:app", host=args.host, port=args.port, reload=args.reload)


def run_standardize_excel(args: argparse.Namespace) -> None:
    out_path = Path(args.out)
    rules = standardize_excel_rules(Path(args.excel), out_path)
    print(f"Standardized {len(rules)} rules to: {out_path}")


def run_import_neo4j(args: argparse.Namespace) -> None:
    from .neo4j_writer import Neo4jWriter

    bundle = load_demo_bundle()
    if args.rules:
        bundle["rules"] = load_rules(Path(args.rules))
    base_cost_rules = load_base_cost_rules()
    report = RuleEngine(**bundle).evaluate()
    writer = Neo4jWriter()
    try:
        writer.write_all(report=report, base_cost_rules=base_cost_rules, **bundle)
    finally:
        writer.close()
    print(f"Imported graph into Neo4j. rules={len(bundle['rules'])}, triggers={len(report.triggers)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smart-kg")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="Run the built-in demo and write a report.")
    demo.add_argument("--out", default=None, help="Output JSON path. Defaults to reports/demo_result.json.")
    demo.set_defaults(func=run_demo)

    serve = subparsers.add_parser("serve", help="Start the FastAPI service.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=run_serve)

    standardize = subparsers.add_parser("standardize-excel", help="Convert Excel BASE rules to standard JSON.")
    standardize.add_argument("--excel", required=True, help="Path to 配置与示例GPKG.xlsx.")
    standardize.add_argument("--out", default="data/standardized/rules_from_excel.json")
    standardize.set_defaults(func=run_standardize_excel)

    neo4j = subparsers.add_parser("import-neo4j", help="Import the graph into Neo4j.")
    neo4j.add_argument("--rules", default=None, help="Rules JSON path. Defaults to data/standardized/rules_from_excel.json when present.")
    neo4j.set_defaults(func=run_import_neo4j)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
