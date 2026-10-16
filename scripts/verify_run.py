#!/usr/bin/env python3
"""Independent verifier CLI. FAIL/BLOCKED always return a nonzero exit code."""
from pathlib import Path
import argparse
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from orbitserve.verification import verify_science, compare_products, verify_run, verify_suite

parser = argparse.ArgumentParser(description=__doc__)
commands = parser.add_subparsers(dest="command", required=True)
science = commands.add_parser("science")
science.add_argument("case_dir")
science.add_argument("output_dir")
comparison = commands.add_parser("compare")
comparison.add_argument("a")
comparison.add_argument("b")
run = commands.add_parser("run")
run.add_argument("run_dir")
suite = commands.add_parser("suite")
suite.add_argument("suite_dir")
parser.add_argument("--output", help="Write complete JSON evidence to this path")
args = parser.parse_args()
if args.command == "science":
    result = verify_science(args.case_dir, args.output_dir)
elif args.command == "compare":
    result = compare_products(args.a, args.b)
elif args.command == "run":
    result = verify_run(args.run_dir)
else:
    result = verify_suite(args.suite_dir)
text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
if args.output:
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(text)
print(text, end="")
raise SystemExit(0 if result["status"] == "PASS" else 1)
