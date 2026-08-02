"""``magpie-eval`` — run the heal-rate eval and report the number (spec 07).

    uv run magpie-eval                       # table + rate, non-zero on drift
    uv run magpie-eval --write               # refresh evals/heal_rate.json
    uv run magpie-eval --json /tmp/out.json  # write elsewhere

Exit codes: ``0`` all cases matched their declared expectation; ``1`` at least one
did not (a regression *or* an unexpected improvement — either way the committed
baseline and the README need updating deliberately).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from magpie.evals.heal_rate import HealRateReport, load_cases, run_suite

DEFAULT_MANIFEST = Path("evals/heal_rate_cases.yaml")
DEFAULT_BASELINE = Path("evals/heal_rate.json")


def _repo_root() -> Path:
    """``src/magpie/evals/cli.py`` → ``parents[3]`` is the repo root."""
    return Path(__file__).resolve().parents[3]


def format_report(report: HealRateReport) -> str:
    """Human-readable table. Pure so it can be asserted on in tests."""
    width = max(len(case.name) for case in report.cases)
    lines = [
        f"{'case'.ljust(width)}  break      before  broken  healed  outcome",
        f"{'-' * width}  ---------  ------  ------  ------  -------",
    ]
    for case in report.cases:
        outcome = "HEALED" if case.healed else "missed"
        flag = "" if case.matches_expectation else "  <- UNEXPECTED"
        lines.append(
            f"{case.name.ljust(width)}  {case.breakage_kind:<9}  "
            f"{case.items_before:>6}  {case.items_broken:>6}  {case.items_healed:>6}  "
            f"{outcome}{flag}"
        )
    lines.append("")
    lines.append(f"heal rate: {report.healed}/{report.total} = {report.heal_rate_pct:.2f}%")
    if report.expectation_failures:
        lines.append("expectation mismatches: " + ", ".join(report.expectation_failures))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    root = _repo_root()
    parser = argparse.ArgumentParser(prog="magpie-eval", description="Heal-rate eval")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / DEFAULT_MANIFEST,
        help=f"case manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument("--json", type=Path, default=None, help="write the report JSON here")
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"write the report to {DEFAULT_BASELINE} (the committed baseline)",
    )
    args = parser.parse_args(argv)

    report = run_suite(load_cases(args.manifest, root=root))
    print(format_report(report))

    payload = json.dumps(report.as_dict(), indent=2, sort_keys=False) + "\n"
    for destination in (args.json, root / DEFAULT_BASELINE if args.write else None):
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(payload, encoding="utf-8")
            print(f"wrote {destination}")

    return 1 if report.expectation_failures else 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    sys.exit(main())
