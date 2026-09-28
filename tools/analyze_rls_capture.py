"""Headless audit for a saved ``高速数据.csv`` capture (older recordings: ``RLS辨识数据.csv``).

The command is intentionally read-only.  It reports the exact R2024b
ESO/ARX replay separately from the closed-loop physical IV result so a finite
RLS coefficient vector cannot be mistaken for a physically valid motor
parameter estimate.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from communications.native_telemetry import run_offline_rls_analysis
from rls_offline import load_rls_capture_csv


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def analyze(path: Path) -> dict[str, object]:
    dataset = load_rls_capture_csv(path)
    analysis = run_offline_rls_analysis(dataset)
    results = list(analysis.get("results", ()))
    final = dict(results[-1]) if results else {}
    physical = dict(analysis.get("physical_iv", {}))
    diagnostics = dict(analysis.get("diagnostics", {}))
    reference = dict(analysis.get("simulink_reference", {}))
    legacy_evidence = dict(analysis.get("legacy_evidence", {}))
    physical_usable = physical.get("verdict") == "usable"
    nominal_match = bool(physical.get("nominal_match", False))
    acceptance_reasons = [
        str(item) for item in (
            list(physical.get("reasons", ())) +
            list(physical.get("nominal_reasons", ())))]
    return {
        "file": str(path.resolve()),
        "sample_count": int(dataset["count"]),
        "rate_hz": int(dataset["rate_hz"]),
        "duration_s": float(dataset["count"]) / float(dataset["rate_hz"]),
        "schema": {
            "direct_id": bool(dataset.get("id_source_direct", False)),
            "direct_sequence": bool(
                dataset.get("sequence_source_direct", False)),
            "direct_vdda": bool(dataset.get("vdda_source_direct", False)),
        },
        "physical_iv": physical,
        "acceptance": {
            "passed": bool(physical_usable and nominal_match),
            "physical_usable": physical_usable,
            "nominal_match": nominal_match,
            "nominal_r_tolerance_percent": 25.0,
            "nominal_l_tolerance_percent": 15.0,
            "reasons": acceptance_reasons,
        },
        "identifiability": diagnostics,
        "legacy_capture_evidence": legacy_evidence,
        "arx_motor_projection": dict(
            analysis.get("arx_motor_projection", {})),
        "arx_motor_cross_check": dict(
            analysis.get("arx_motor_cross_check", {})),
        "simulink_reference": reference,
        "simulink_eso_rls_final": {
            "updates": int(final.get("updates", 0)),
            "theta_d": list(final.get("theta_d", ())),
            "theta_q": list(final.get("theta_q", ())),
            "low_frequency_moment_diagnostic": {
                "physical_validated": False,
                "rd_ohm": _finite_or_none(final.get("rd_ohm")),
                "rq_ohm": _finite_or_none(final.get("rq_ohm")),
                "ld_mh": _finite_or_none(final.get("ld_mh")),
                "lq_mh": _finite_or_none(final.get("lq_mh")),
            },
        },
    }


def main() -> int:
    # Windows terminals commonly inherit a legacy code page while Codex and
    # redirected logs expect UTF-8.  Keep Chinese diagnostics readable in both.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="只读审计真机RLS CSV，不修改或投影辨识结果")
    parser.add_argument("csv", type=Path, help="高速数据.csv（旧记录为 RLS辨识数据.csv）路径")
    parser.add_argument(
        "--compact", action="store_true", help="输出单行 JSON")
    parser.add_argument(
        "--require-nominal", action="store_true",
        help="物理辨识或标称一致性未通过时返回退出码1")
    args = parser.parse_args()
    try:
        report = analyze(args.csv)
    except Exception as exc:  # CLI boundary: preserve a concise nonzero exit.
        print(f"RLS capture analysis failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(
        report, ensure_ascii=False, allow_nan=False,
        indent=None if args.compact else 2))
    if args.require_nominal and not report["acceptance"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
