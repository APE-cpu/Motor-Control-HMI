"""历史遥测的基础统计。"""
from __future__ import annotations

import math
from typing import Any


METRIC_FIELDS = (
    "speed_actual", "current_actual", "torque_actual", "vdc", "temperature",
)


def summarize_telemetry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """计算可复现的描述统计；空列返回 count=0。"""
    summary: dict[str, Any] = {"samples": len(rows), "duration_s": 0.0, "metrics": {}}
    times = [_number(row.get("monotonic_s")) for row in rows]
    valid_times = [value for value in times if value is not None]
    if len(valid_times) >= 2:
        summary["duration_s"] = max(valid_times) - min(valid_times)
    for field in METRIC_FIELDS:
        values = [_number(row.get(field)) for row in rows]
        valid = [value for value in values if value is not None]
        if not valid:
            summary["metrics"][field] = {"count": 0}
            continue
        mean = sum(valid) / len(valid)
        rms = math.sqrt(sum(value * value for value in valid) / len(valid))
        summary["metrics"][field] = {
            "count": len(valid),
            "min": min(valid),
            "max": max(valid),
            "mean": mean,
            "rms": rms,
            "peak_to_peak": max(valid) - min(valid),
        }
    errors = []
    for row in rows:
        actual = _number(row.get("speed_actual"))
        target = _number(row.get("speed_target"))
        if actual is not None and target is not None:
            errors.append(abs(actual - target))
    summary["speed_mae_rpm"] = sum(errors) / len(errors) if errors else None
    return summary


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
