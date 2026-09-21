"""离线辨识数据集的同步缓冲、导出、校验与物理验证。

绘图曲线为了显示会各自限长并且分开存储，不能反向拼成辨识数据。
本模块保留 F1 在同一帧中的所有原始量，供离线 C++ RLS 重放和
闭环工具变量 R/L 验证共同使用。
"""
from __future__ import annotations

import csv
import math
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


RLS_FLOAT_FIELDS = (
    "angle_deg", "speed_rpm", "iq_a", "iqref_a", "ia_a", "ib_a",
    "vd_raw", "vq_raw", "vbus_v",
)
RLS_OPTIONAL_FLOAT_FIELDS = ("id_a", "idref_a")
RLS_OPTIONAL_META_FLOAT_FIELDS = ("vdda_v",)
RLS_OPTIONAL_INT_FIELDS = ("sample_seq",)
RLS_OPTIONAL_APPLIED_FLOAT_FIELDS = (
    "actuation_angle_deg", "vd_applied_v", "vq_applied_v",
)
RLS_OPTIONAL_APPLIED_INT_FIELDS = ("duty_a", "duty_b", "duty_c")
RLS_REQUIRED_FIELDS = ("tick_ms", *RLS_FLOAT_FIELDS)
RLS_CSV_FIELDS = (
    "sample_index", "time_s", "tick_ms", "sample_seq", "rate_hz",
    "angle_deg", "speed_rpm", "iq_a", "id_a", "iqref_a", "idref_a", "ia_a",
    "ib_a", "vd_raw", "vq_raw", "vbus_v", "vdda_v",
    "actuation_angle_deg", "duty_a", "duty_b", "duty_c",
    "vd_applied_v", "vq_applied_v",
)


@dataclass(frozen=True)
class RlsCaptureSnapshot:
    """只读的分批快照；批内 array 一旦入队后不再修改。"""

    rate_hz: int
    sample_count: int
    dropped_samples: int
    batches: tuple[dict[str, array], ...]

    @property
    def duration_s(self) -> float:
        return (self.sample_count / self.rate_hz
                if self.rate_hz > 0 else 0.0)

    def to_columns(self) -> dict[str, object]:
        """Flatten the immutable batches for direct background analysis.

        This avoids the previous write-100MB/read-100MB round trip.  The copy
        is deliberate: native replay requires contiguous aligned columns, and
        constructing them in the worker thread keeps the GUI responsive.
        """
        if self.sample_count <= 0 or self.rate_hz <= 0:
            raise ValueError("暂无可用的同步 F1 辨识数据")
        included_optional = {
            name for name in RLS_OPTIONAL_FLOAT_FIELDS
            if self.batches and all(name in batch for batch in self.batches)}
        included_optional_int = {
            name for name in RLS_OPTIONAL_INT_FIELDS
            if self.batches and all(name in batch for batch in self.batches)}
        included_optional_meta = {
            name for name in RLS_OPTIONAL_META_FLOAT_FIELDS
            if self.batches and all(name in batch for batch in self.batches)}
        included_applied_float = {
            name for name in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS
            if self.batches and all(name in batch for batch in self.batches)}
        included_applied_int = {
            name for name in RLS_OPTIONAL_APPLIED_INT_FIELDS
            if self.batches and all(name in batch for batch in self.batches)}
        columns: dict[str, object] = {
            "count": self.sample_count,
            "rate_hz": self.rate_hz,
            "dropped_samples": self.dropped_samples,
            **{name: array("I" if name == "tick_ms" else "f")
               for name in RLS_REQUIRED_FIELDS},
            **{name: array("f") for name in included_optional},
            **{name: array("H") for name in included_optional_int},
            **{name: array("f") for name in included_optional_meta},
            **{name: array("f") for name in included_applied_float},
            **{name: array("H") for name in included_applied_int},
        }
        for batch in self.batches:
            for name in (*RLS_REQUIRED_FIELDS, *included_optional,
                         *included_optional_int, *included_optional_meta,
                         *included_applied_float, *included_applied_int):
                columns[name].extend(batch[name])  # type: ignore[union-attr,index]
        columns["id_source_direct"] = (
            set(RLS_OPTIONAL_FLOAT_FIELDS) <= included_optional)
        columns["sequence_source_direct"] = (
            set(RLS_OPTIONAL_INT_FIELDS) <= included_optional_int)
        columns["vdda_source_direct"] = (
            set(RLS_OPTIONAL_META_FLOAT_FIELDS) <= included_optional_meta)
        columns["applied_voltage_source_direct"] = (
            set(RLS_OPTIONAL_APPLIED_FLOAT_FIELDS) <= included_applied_float and
            set(RLS_OPTIONAL_APPLIED_INT_FIELDS) <= included_applied_int)
        validate_rls_columns(columns)
        return columns

    def write_csv(self, path: str | Path) -> None:
        if self.sample_count <= 0 or self.rate_hz <= 0:
            raise ValueError("暂无可用的同步 F1 辨识数据")
        with open(path, "w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            included_optional = {
                name for name in RLS_OPTIONAL_FLOAT_FIELDS
                if bool(self.batches) and all(
                    name in batch for batch in self.batches)}
            included_optional_int = {
                name for name in RLS_OPTIONAL_INT_FIELDS
                if bool(self.batches) and all(
                    name in batch for batch in self.batches)}
            included_optional_meta = {
                name for name in RLS_OPTIONAL_META_FLOAT_FIELDS
                if bool(self.batches) and all(
                    name in batch for batch in self.batches)}
            included_applied_float = {
                name for name in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS
                if bool(self.batches) and all(
                    name in batch for batch in self.batches)}
            included_applied_int = {
                name for name in RLS_OPTIONAL_APPLIED_INT_FIELDS
                if bool(self.batches) and all(
                    name in batch for batch in self.batches)}
            fields = tuple(name for name in RLS_CSV_FIELDS
                           if ((name not in RLS_OPTIONAL_FLOAT_FIELDS or
                                 name in included_optional) and
                               (name not in RLS_OPTIONAL_META_FLOAT_FIELDS or
                                name in included_optional_meta) and
                               (name not in RLS_OPTIONAL_INT_FIELDS or
                                name in included_optional_int) and
                               (name not in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS or
                                name in included_applied_float) and
                               (name not in RLS_OPTIONAL_APPLIED_INT_FIELDS or
                                name in included_applied_int)))
            writer.writerow(fields)
            sample_index = 0
            for batch in self.batches:
                count = len(batch["tick_ms"])
                for offset in range(count):
                    row = {
                        "sample_index": sample_index,
                        "time_s": f"{sample_index / self.rate_hz:.9f}",
                        "tick_ms": int(batch["tick_ms"][offset]),
                        "rate_hz": self.rate_hz,
                        **{name: f"{float(batch[name][offset]):.9g}"
                           for name in RLS_FLOAT_FIELDS},
                    }
                    for name in included_optional_int:
                        row[name] = int(batch[name][offset])
                    for name in included_optional:
                        row[name] = f"{float(batch[name][offset]):.9g}"
                    for name in included_optional_meta:
                        row[name] = f"{float(batch[name][offset]):.9g}"
                    for name in included_applied_float:
                        row[name] = f"{float(batch[name][offset]):.9g}"
                    for name in included_applied_int:
                        row[name] = int(batch[name][offset])
                    writer.writerow([row[name] for name in fields])
                    sample_index += 1


def validate_rls_columns(columns: Mapping[str, object]) -> int:
    """校验逐帧对齐的 RLS 输入，返回样本数。"""
    try:
        count = int(columns.get("count", len(columns["tick_ms"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("缺少有效的 count/tick_ms") from exc
    if count <= 0:
        raise ValueError("辨识数据没有样本")
    try:
        rate_hz = int(columns["rate_hz"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("缺少有效的 rate_hz") from exc
    if rate_hz <= 0:
        raise ValueError("rate_hz 必须大于 0")
    for name in RLS_REQUIRED_FIELDS:
        if name not in columns:
            raise ValueError(f"辨识数据缺少必需列 {name}")
        values = columns[name]
        try:
            length = len(values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"{name} 不是可用的样本列") from exc
        if length != count:
            raise ValueError(
                f"{name} 有 {length} 点，与 count={count} 不对齐")
        for value in values:  # type: ignore[assignment]
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} 包含 NaN/Inf")
    for name in RLS_OPTIONAL_FLOAT_FIELDS:
        if name in columns:
            if len(columns[name]) != count:  # type: ignore[arg-type]
                raise ValueError(f"{name} 与 count 不对齐")
            if any(not math.isfinite(float(value))
                   for value in columns[name]):  # type: ignore[assignment]
                raise ValueError(f"{name} 包含 NaN/Inf")
    for name in RLS_OPTIONAL_META_FLOAT_FIELDS:
        if name in columns:
            if len(columns[name]) != count:  # type: ignore[arg-type]
                raise ValueError(f"{name} 与 count 不对齐")
            if any(not math.isfinite(float(value))
                   for value in columns[name]):  # type: ignore[assignment]
                raise ValueError(f"{name} 包含 NaN/Inf")
    for name in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS:
        if name in columns:
            if len(columns[name]) != count:  # type: ignore[arg-type]
                raise ValueError(f"{name} 与 count 不对齐")
            if any(not math.isfinite(float(value))
                   for value in columns[name]):  # type: ignore[assignment]
                raise ValueError(f"{name} 包含 NaN/Inf")
    for name in RLS_OPTIONAL_INT_FIELDS:
        if name in columns and len(columns[name]) != count:  # type: ignore[arg-type]
            raise ValueError(f"{name} 与 count 不对齐")
    for name in RLS_OPTIONAL_APPLIED_INT_FIELDS:
        if name in columns and len(columns[name]) != count:  # type: ignore[arg-type]
            raise ValueError(f"{name} 与 count 不对齐")
    if any(float(value) <= 1.0 for value in columns["vbus_v"]):
        raise ValueError("vbus_v 包含无效母线电压（必须 >1 V）")
    return count


def resolve_voltage_columns_si(columns: Mapping[str, object]):
    """Return dq voltage in volts and its provenance.

    F1/40 carries the final PWM compare values reconstructed into dq volts by
    the native parser.  Older captures only contain limited controller-command
    digits, so they remain a compatibility fallback and are labelled as such.
    """
    import numpy as np

    if (bool(columns.get("applied_voltage_source_direct", False)) and
            all(name in columns for name in
                ("vd_applied_v", "vq_applied_v"))):
        return (
            np.asarray(columns["vd_applied_v"], dtype=float),
            np.asarray(columns["vq_applied_v"], dtype=float),
            "pwm_duty_reconstructed",
        )
    vbus_v = np.asarray(columns["vbus_v"], dtype=float)
    volts_per_digit = vbus_v / (math.sqrt(3.0) * 32768.0)
    return (
        np.asarray(columns["vd_raw"], dtype=float) * volts_per_digit,
        np.asarray(columns["vq_raw"], dtype=float) * volts_per_digit,
        "limited_controller_command",
    )


class RlsCaptureBuffer:
    """以紧凑 array 分批保留最近一段同步 F1 数据。"""

    def __init__(self, max_seconds: float = 60.0) -> None:
        self.max_seconds = max(1.0, float(max_seconds))
        self.rate_hz = 0
        self.sample_count = 0
        self.dropped_samples = 0
        self._batches: deque[dict[str, array]] = deque()

    def clear(self) -> None:
        self.rate_hz = 0
        self.sample_count = 0
        self.dropped_samples = 0
        self._batches.clear()

    @property
    def duration_s(self) -> float:
        return self.sample_count / self.rate_hz if self.rate_hz > 0 else 0.0

    def snapshot(self) -> RlsCaptureSnapshot:
        """快速固化当前批次引用，不复制数十 MB 样本数组。"""
        return RlsCaptureSnapshot(
            rate_hz=self.rate_hz,
            sample_count=self.sample_count,
            dropped_samples=self.dropped_samples,
            batches=tuple(self._batches),
        )

    def append_columns(self, columns: Mapping[str, object]) -> bool:
        try:
            count = int(columns.get("count", 0))
            rate_hz = int(columns.get("rate_hz", 0))
        except (TypeError, ValueError):
            return False
        if count <= 0 or rate_hz <= 0:
            return False
        batch: dict[str, array] = {}
        for name in RLS_REQUIRED_FIELDS:
            values = columns.get(name)
            if values is None or len(values) < count:  # type: ignore[arg-type]
                return False
            typecode = "I" if name == "tick_ms" else "f"
            try:
                batch[name] = array(typecode, values[:count])  # type: ignore[index]
            except (TypeError, ValueError, OverflowError):
                return False
        if bool(columns.get("id_source_direct", False)):
            for name in RLS_OPTIONAL_FLOAT_FIELDS:
                values = columns.get(name)
                if values is None or len(values) < count:  # type: ignore[arg-type]
                    return False
                try:
                    batch[name] = array("f", values[:count])  # type: ignore[index]
                except (TypeError, ValueError, OverflowError):
                    return False
                if any(not math.isfinite(value) for value in batch[name]):
                    return False
        if bool(columns.get("sequence_source_direct", False)):
            values = columns.get("sample_seq")
            if values is None or len(values) < count:  # type: ignore[arg-type]
                return False
            try:
                batch["sample_seq"] = array(
                    "H", (int(value) & 0xFFFF for value in values[:count]))  # type: ignore[index]
            except (TypeError, ValueError, OverflowError):
                return False
        if bool(columns.get("vdda_source_direct", False)):
            for name in RLS_OPTIONAL_META_FLOAT_FIELDS:
                values = columns.get(name)
                if values is None or len(values) < count:  # type: ignore[arg-type]
                    return False
                try:
                    batch[name] = array("f", values[:count])  # type: ignore[index]
                except (TypeError, ValueError, OverflowError):
                    return False
                if any(not math.isfinite(value) for value in batch[name]):
                    return False
        if bool(columns.get("applied_voltage_source_direct", False)):
            for name in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS:
                values = columns.get(name)
                if values is None or len(values) < count:  # type: ignore[arg-type]
                    return False
                try:
                    batch[name] = array("f", values[:count])  # type: ignore[index]
                except (TypeError, ValueError, OverflowError):
                    return False
                if any(not math.isfinite(value) for value in batch[name]):
                    return False
            for name in RLS_OPTIONAL_APPLIED_INT_FIELDS:
                values = columns.get(name)
                if values is None or len(values) < count:  # type: ignore[arg-type]
                    return False
                try:
                    batch[name] = array(
                        "H", (int(value) & 0xFFFF
                              for value in values[:count]))  # type: ignore[index]
                except (TypeError, ValueError, OverflowError):
                    return False
        if any(not math.isfinite(value)
               for name in RLS_FLOAT_FIELDS for value in batch[name]):
            return False
        if any(value <= 1.0 for value in batch["vbus_v"]):
            return False
        if self.rate_hz and self.rate_hz != rate_hz:
            # 一份数据集只允许一个离散采样周期。
            self.clear()
        self.rate_hz = rate_hz
        self._batches.append(batch)
        self.sample_count += count
        max_samples = max(1, int(self.max_seconds * self.rate_hz))
        while self.sample_count > max_samples and len(self._batches) > 1:
            removed = self._batches.popleft()
            removed_count = len(removed["tick_ms"])
            self.sample_count -= removed_count
            self.dropped_samples += removed_count
        return True

    def write_csv(self, path: str | Path) -> None:
        self.snapshot().write_csv(path)


def load_rls_capture_csv(path: str | Path) -> dict[str, object]:
    """读取 ``RLS辨识数据.csv`` 并验证采样率与行对齐。"""
    columns: dict[str, array] = {
        "sample_index": array("I"),
        "tick_ms": array("I"),
        **{name: array("d") for name in RLS_FLOAT_FIELDS},
    }
    rate_hz = 0
    with open(path, newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required_csv_fields = tuple(
            name for name in RLS_CSV_FIELDS
            if (name not in RLS_OPTIONAL_FLOAT_FIELDS and
                name not in RLS_OPTIONAL_META_FLOAT_FIELDS and
                name not in RLS_OPTIONAL_INT_FIELDS and
                name not in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS and
                name not in RLS_OPTIONAL_APPLIED_INT_FIELDS))
        missing = [name for name in required_csv_fields
                   if name not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(
                "这不是同步 RLS 辨识文件，缺少列：" + ", ".join(missing))
        has_direct_id = all(name in (reader.fieldnames or ())
                            for name in RLS_OPTIONAL_FLOAT_FIELDS)
        if has_direct_id:
            for name in RLS_OPTIONAL_FLOAT_FIELDS:
                columns[name] = array("d")
        has_sample_sequence = "sample_seq" in (reader.fieldnames or ())
        if has_sample_sequence:
            columns["sample_seq"] = array("H")
        has_vdda = all(name in (reader.fieldnames or ())
                       for name in RLS_OPTIONAL_META_FLOAT_FIELDS)
        if has_vdda:
            for name in RLS_OPTIONAL_META_FLOAT_FIELDS:
                columns[name] = array("d")
        has_applied_voltage = (
            all(name in (reader.fieldnames or ())
                for name in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS) and
            all(name in (reader.fieldnames or ())
                for name in RLS_OPTIONAL_APPLIED_INT_FIELDS))
        if has_applied_voltage:
            for name in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS:
                columns[name] = array("d")
            for name in RLS_OPTIONAL_APPLIED_INT_FIELDS:
                columns[name] = array("H")
        for row_number, row in enumerate(reader, start=2):
            try:
                row_rate = int(float(row["rate_hz"]))
                if rate_hz == 0:
                    rate_hz = row_rate
                elif row_rate != rate_hz:
                    raise ValueError("文件中采样率发生变化")
                columns["sample_index"].append(int(row["sample_index"]))
                columns["tick_ms"].append(int(float(row["tick_ms"])))
                if has_sample_sequence:
                    columns["sample_seq"].append(
                        int(float(row["sample_seq"])) & 0xFFFF)
                for name in RLS_FLOAT_FIELDS:
                    columns[name].append(float(row[name]))
                if has_direct_id:
                    for name in RLS_OPTIONAL_FLOAT_FIELDS:
                        columns[name].append(float(row[name]))
                if has_vdda:
                    for name in RLS_OPTIONAL_META_FLOAT_FIELDS:
                        columns[name].append(float(row[name]))
                if has_applied_voltage:
                    for name in RLS_OPTIONAL_APPLIED_FLOAT_FIELDS:
                        columns[name].append(float(row[name]))
                    for name in RLS_OPTIONAL_APPLIED_INT_FIELDS:
                        columns[name].append(
                            int(float(row[name])) & 0xFFFF)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"第 {row_number} 行无效：{exc}") from exc
    output: dict[str, object] = {
        "count": len(columns["tick_ms"]),
        "rate_hz": rate_hz,
        "id_source_direct": has_direct_id,
        "sequence_source_direct": has_sample_sequence,
        "vdda_source_direct": has_vdda,
        "applied_voltage_source_direct": has_applied_voltage,
        **columns,
    }
    validate_rls_columns(output)
    return output


def assess_rls_identifiability(
        columns: Mapping[str, object], *, pole_pairs: int = 4,
        nominal_r_ohm: float = 0.59,
        nominal_l_h: float = 0.00066,
        nominal_flux_wb: float = 0.00585) -> dict[str, object]:
    """评估真机 CSV 是否具备从闭环 dq 数据辨识 R/L 的条件。

    这里只做证据诊断，不修改、投影或“修正”RLS系数。
    """
    count = validate_rls_columns(columns)
    import numpy as np

    rate_hz = float(columns["rate_hz"])
    if count < max(64, int(rate_hz * 0.02)):
        return {
            "verdict": "not_identifiable",
            "reasons": [
                f"数据仅 {count} 点，少于20 ms的可辨识性检查下限"],
            "sample_count": count,
            "rate_hz": int(rate_hz),
            "duration_s": count / rate_hz,
        }
    angle = np.deg2rad(np.asarray(columns["angle_deg"], dtype=float))
    ia = np.asarray(columns["ia_a"], dtype=float)
    ib = np.asarray(columns["ib_a"], dtype=float)
    beta = -(ia + 2.0 * ib) / math.sqrt(3.0)
    direct_id = bool(columns.get("id_source_direct", "id_a" in columns))
    id_a = (np.asarray(columns["id_a"], dtype=float) if direct_id else
            ia * np.sin(angle) + beta * np.cos(angle))
    iq_a = np.asarray(columns["iq_a"], dtype=float)
    speed_rpm = np.asarray(columns["speed_rpm"], dtype=float)
    ud_v, uq_v, voltage_source = resolve_voltage_columns_si(columns)

    # Keep the diagnostic bounded for long 16 kHz captures while preserving
    # the one/two/three-sample regressor geometry.
    stride = max(1, count // 100000)
    indices = np.arange(4, count, stride)
    phi_d = np.column_stack((
        id_a[indices - 1], id_a[indices - 2], id_a[indices - 3],
        ud_v[indices - 2], ud_v[indices - 3],
        uq_v[indices - 2], uq_v[indices - 3],
    ))
    phi_q = np.column_stack((
        iq_a[indices - 1], iq_a[indices - 2], iq_a[indices - 3],
        ud_v[indices - 2], ud_v[indices - 3],
        uq_v[indices - 2], uq_v[indices - 3],
    ))

    def normalized_condition(matrix) -> float:
        scale = np.std(matrix, axis=0)
        normalized = (matrix - np.mean(matrix, axis=0)) / np.where(
            scale > 1e-15, scale, 1.0)
        return float(np.linalg.cond(normalized))

    arx_condition_d = normalized_condition(phi_d)
    arx_condition_q = normalized_condition(phi_q)

    # About 8 ms of averaging exposes commanded low-frequency excitation and
    # suppresses the current-loop/PWM noise that must not be mistaken for it.
    decimation = 8
    slow_window = max(2, int(round(rate_hz * 0.008)))
    kernel = np.ones(slow_window, dtype=float) / slow_window
    # Filter before decimation; reversing the order aliases PWM/current-loop
    # ripple into a false low-frequency "excitation" measurement.
    id_slow = np.convolve(id_a, kernel, mode="valid")[::decimation]
    iq_slow = np.convolve(iq_a, kernel, mode="valid")[::decimation]
    speed_slow = np.convolve(
        speed_rpm, kernel, mode="valid")[::decimation]
    id_excitation_std_a = float(np.std(id_slow))
    iq_excitation_std_a = float(np.std(iq_slow))
    iq_slow_std = float(np.std(iq_slow))
    speed_slow_std = float(np.std(speed_slow))
    # corrcoef divides by both standard deviations.  A constant-speed capture
    # is a valid operating point, but its correlation is mathematically
    # undefined; report that explicitly instead of emitting NumPy warnings.
    iq_speed_correlation = (
        float(np.corrcoef(iq_slow, speed_slow)[0, 1])
        if iq_slow_std > 1e-12 and speed_slow_std > 1e-12
        else math.nan
    )

    # Compare the recorded controller voltage with the nominal PMSM voltage
    # equations. A large normalized residual indicates that command voltage is
    # not a sufficiently faithful motor-terminal voltage for physical fitting.
    filter_window = 16
    filter_kernel = np.ones(filter_window, dtype=float) / filter_window
    id_filtered = np.convolve(id_a, filter_kernel, mode="same")
    iq_filtered = np.convolve(iq_a, filter_kernel, mode="same")
    did_dt = np.gradient(id_filtered) * rate_hz
    diq_dt = np.gradient(iq_filtered) * rate_hz
    omega_e = speed_rpm * max(1, int(pole_pairs)) * 2.0 * math.pi / 60.0
    ud_nominal = (nominal_r_ohm * id_filtered +
                  nominal_l_h * did_dt - omega_e * nominal_l_h * iq_filtered)
    uq_nominal = (nominal_r_ohm * iq_filtered + nominal_l_h * diq_dt +
                  omega_e * (nominal_l_h * id_filtered + nominal_flux_wb))
    valid = np.arange(filter_window, count - filter_window, stride)
    ud_rmse_v = float(np.sqrt(np.mean(
        (ud_v[valid] - ud_nominal[valid]) ** 2)))
    uq_rmse_v = float(np.sqrt(np.mean(
        (uq_v[valid] - uq_nominal[valid]) ** 2)))
    ud_nrmse = ud_rmse_v / max(float(np.std(ud_v[valid])), 1e-12)
    uq_nrmse = uq_rmse_v / max(float(np.std(uq_v[valid])), 1e-12)

    reasons: list[str] = []
    if id_excitation_std_a < 0.05:
        reasons.append(
            f"d轴慢变激励仅 {id_excitation_std_a:.3f} A，"
            "不足以分离 Rd/Ld")
    if math.isfinite(iq_speed_correlation) and abs(iq_speed_correlation) > 0.95:
        reasons.append(
            f"q轴电流与转速相关系数 {iq_speed_correlation:.3f}，"
            "电阻与反电动势项近似共线")
    if max(arx_condition_d, arx_condition_q) > 100.0:
        reasons.append(
            f"ARX归一化条件数 d/q={arx_condition_d:.1f}/"
            f"{arx_condition_q:.1f}，系数对噪声高敏感")
    if max(ud_nrmse, uq_nrmse) > 0.5:
        reasons.append(
            f"施加电压与标称端电压模型残差过大"
            f"(NRMSE d/q={ud_nrmse:.2f}/{uq_nrmse:.2f})")

    return {
        "verdict": "not_identifiable" if reasons else "usable",
        "reasons": reasons,
        "sample_count": count,
        "rate_hz": int(rate_hz),
        "duration_s": count / rate_hz,
        "id_mean_a": float(np.mean(id_a)),
        "id_source": "firmware_park" if direct_id else "phase_reconstructed",
        "id_rms_a": float(np.sqrt(np.mean(id_a * id_a))),
        "iq_rms_a": float(np.sqrt(np.mean(iq_a * iq_a))),
        "id_excitation_std_a": id_excitation_std_a,
        "iq_excitation_std_a": iq_excitation_std_a,
        "iq_speed_correlation": iq_speed_correlation,
        "arx_condition_d": arx_condition_d,
        "arx_condition_q": arx_condition_q,
        "nominal_ud_rmse_v": ud_rmse_v,
        "nominal_uq_rmse_v": uq_rmse_v,
        "nominal_ud_nrmse": ud_nrmse,
        "nominal_uq_nrmse": uq_nrmse,
        "voltage_source": voltage_source,
    }


def assess_legacy_capture_evidence(
        columns: Mapping[str, object], *, pole_pairs: int = 4) -> dict[str, object]:
    """Extract the limited physical evidence that an old F1 CSV still carries.

    Legacy files lack direct Id/IdRef, a firmware sample sequence and measured
    VDDA.  They therefore cannot pass the independent closed-loop IV test.
    They *can* still prove dq reconstruction consistency and provide a
    quasi-steady command-frame PMSM fit over a speed ramp.  The latter is
    deliberately labelled diagnostic-only: controller voltage is not measured
    terminal voltage and the fit cannot separate Ld from Lq.
    """
    count = validate_rls_columns(columns)
    if bool(columns.get("id_source_direct", False)):
        return {"verdict": "not_applicable", "reasons": []}

    import numpy as np

    rate_hz = int(columns["rate_hz"])
    if count < rate_hz * 3:
        return {
            "verdict": "insufficient_data",
            "reasons": ["旧格式诊断至少需要3 s连续数据"],
        }

    angle = np.deg2rad(np.asarray(columns["angle_deg"], dtype=float))
    ia = np.asarray(columns["ia_a"], dtype=float)
    ib = np.asarray(columns["ib_a"], dtype=float)
    beta = -(ia + 2.0 * ib) / math.sqrt(3.0)
    id_a = ia * np.sin(angle) + beta * np.cos(angle)
    iq_from_phase = ia * np.cos(angle) - beta * np.sin(angle)
    iq_a = np.asarray(columns["iq_a"], dtype=float)

    # A gain/constant fit distinguishes a harmless current-scale rounding
    # difference from an angle or sample-alignment error.
    stride = max(1, count // 100000)
    park_design = np.column_stack((
        iq_from_phase[::stride], id_a[::stride],
        np.ones_like(iq_from_phase[::stride])))
    park_target = iq_a[::stride]
    park_coeff, _, _, _ = np.linalg.lstsq(
        park_design, park_target, rcond=None)
    park_prediction = park_design @ park_coeff
    park_rmse = float(np.sqrt(np.mean(
        (park_prediction - park_target) ** 2)))
    park_correlation = float(np.corrcoef(
        park_prediction, park_target)[0, 1])
    park_gain = float(math.hypot(park_coeff[0], park_coeff[1]))
    park_angle_offset_deg = float(math.degrees(math.atan2(
        -park_coeff[1], park_coeff[0])))

    speed_rpm = np.asarray(columns["speed_rpm"], dtype=float)
    omega_e = speed_rpm * max(1, int(pole_pairs)) * 2.0 * math.pi / 60.0
    ud_v, uq_v, voltage_source = resolve_voltage_columns_si(columns)

    candidates: list[dict[str, float]] = []
    for block_seconds in (0.05, 0.10, 0.20):
        block = max(8, int(round(rate_hz * block_seconds)))
        blocks = count // block
        if blocks < 12:
            continue

        def block_mean(values):
            return values[:blocks * block].reshape(blocks, block).mean(axis=1)

        id_mean = block_mean(id_a)
        iq_mean = block_mean(iq_a)
        ud_mean = block_mean(ud_v)
        uq_mean = block_mean(uq_v)
        omega_mean = block_mean(omega_e)
        dt = block / rate_hz
        did_dt = np.gradient(id_mean, dt)
        diq_dt = np.gradient(iq_mean, dt)
        times = (np.arange(blocks) + 0.5) * dt
        for start_s in (0.0, 5.0):
            selected = times >= start_s
            if np.count_nonzero(selected) < 10:
                continue
            # Surface-PMSM command-frame diagnostic.  One shared L is fitted
            # because the old capture has no independent d-axis excitation.
            d_design = np.column_stack((
                id_mean, did_dt - omega_mean * iq_mean,
                np.zeros(blocks), -omega_mean))
            q_design = np.column_stack((
                iq_mean, diq_dt + omega_mean * id_mean,
                omega_mean, np.zeros(blocks)))
            design = np.vstack((d_design[selected], q_design[selected]))
            target = np.concatenate((ud_mean[selected], uq_mean[selected]))
            coefficient, _, rank, _ = np.linalg.lstsq(
                design, target, rcond=None)
            if rank < 4 or not np.all(np.isfinite(coefficient)):
                continue
            resistance, inductance, flux_d, flux_q = (
                float(value) for value in coefficient)
            prediction = design @ coefficient
            voltage_rmse = float(np.sqrt(np.mean(
                (prediction - target) ** 2)))
            speed_design = np.column_stack((
                iq_mean[selected], np.ones(np.count_nonzero(selected))))
            speed_coefficient, _, _, _ = np.linalg.lstsq(
                speed_design, omega_mean[selected], rcond=None)
            speed_prediction = speed_design @ speed_coefficient
            speed_iq_correlation = float(np.corrcoef(
                iq_mean[selected], omega_mean[selected])[0, 1])
            scale = np.std(design, axis=0)
            normalized = (design - np.mean(design, axis=0)) / np.where(
                scale > 1e-15, scale, 1.0)
            candidates.append({
                "block_ms": block_seconds * 1000.0,
                "start_s": start_s,
                "resistance_ohm": resistance,
                "shared_inductance_mh": inductance * 1e3,
                "flux_wb": math.hypot(flux_d, flux_q),
                "flux_angle_offset_deg": math.degrees(math.atan2(
                    flux_q, flux_d)),
                "voltage_rmse_v": voltage_rmse,
                "normalized_condition": float(np.linalg.cond(normalized)),
                "omega_iq_slope_rad_s_per_a": float(speed_coefficient[0]),
                "omega_iq_intercept_rad_s": float(speed_coefficient[1]),
                "omega_iq_correlation": speed_iq_correlation,
                "omega_iq_fit_rmse_rad_s": float(np.sqrt(np.mean(
                    (speed_prediction - omega_mean[selected]) ** 2))),
            })

    reasons = [
        "旧格式没有直接Id/IdRef和独立d/q激励，不能分离Ld/Lq",
        "保存的是PI限幅后的电压指令，不是电机端实测电压",
        "升速过程中Iq与转速高度相关，R与永磁磁链近似共线",
    ]
    output: dict[str, object] = {
        "verdict": "diagnostic_only",
        "reasons": reasons,
        "voltage_source": voltage_source,
        "park_reconstruction": {
            "iq_rmse_a": park_rmse,
            "correlation": park_correlation,
            "gain": park_gain,
            "angle_offset_deg": park_angle_offset_deg,
        },
        "fits": candidates,
    }
    if candidates:
        for key in ("resistance_ohm", "shared_inductance_mh", "flux_wb",
                    "flux_angle_offset_deg", "voltage_rmse_v",
                    "normalized_condition", "omega_iq_slope_rad_s_per_a",
                    "omega_iq_intercept_rad_s", "omega_iq_correlation",
                    "omega_iq_fit_rmse_rad_s"):
            values = np.asarray([item[key] for item in candidates], dtype=float)
            output[f"{key}_median"] = float(np.median(values))
            output[f"{key}_range"] = (
                float(np.min(values)), float(np.max(values)))
    return output


def derive_arx7_motor_parameter_diagnostic(
        theta_d, theta_q, *, sample_time_s: float) -> dict[str, object]:
    """Project the two seven-term ARX models onto equivalent first-order R/L.

    For one axis, with ``q = z^-1``::

        H(q) = (b0*q + b1*q^2) /
               (1 - a1*q - a2*q^2 - a3*q^3)

    matching the DC gain and its first moment to the discrete first-order RL
    form gives ``R=(1-sum(a))/(b0+b1)`` and the moment expression below.  The
    expression exactly reduces to ``R=(1-a1)/b0, L=Ts/b0`` for the original
    forward-Euler one-pole model.  This is only a reduced-order projection of
    the identified transfer function, not a unique physical inversion of the
    coupled PMSM model.

    Flux is intentionally not fabricated: the seven regressors contain only
    delayed current and d/q voltage.  Without speed/back-EMF, a constant term,
    or the ESO disturbance state, permanent-magnet flux is structurally
    unobservable from these coefficients.
    """
    try:
        d = [float(value) for value in theta_d]
        q = [float(value) for value in theta_q]
        sample_time_s = float(sample_time_s)
    except (TypeError, ValueError):
        return {
            "verdict": "invalid",
            "reasons": ["ARX系数或采样周期不是有效数值"],
        }
    if len(d) != 7 or len(q) != 7 or sample_time_s <= 0.0 or not all(
            math.isfinite(value) for value in (*d, *q, sample_time_s)):
        return {
            "verdict": "invalid",
            "reasons": ["需要d/q各7个有限ARX系数和正采样周期"],
        }

    def project(values, own_index: int, cross_index: int) -> dict[str, object]:
        a1, a2, a3 = values[:3]
        b0, b1 = values[own_index:own_index + 2]
        cross_b0, cross_b1 = values[cross_index:cross_index + 2]
        denominator_dc = 1.0 - a1 - a2 - a3
        own_numerator_dc = b0 + b1
        cross_numerator_dc = cross_b0 + cross_b1
        output: dict[str, object] = {
            "denominator_dc": denominator_dc,
            "own_numerator_dc": own_numerator_dc,
            "cross_numerator_dc": cross_numerator_dc,
            "own_dc_gain_a_per_v": float("nan"),
            "cross_dc_gain_a_per_v": float("nan"),
            "resistance_ohm": float("nan"),
            "inductance_mh": float("nan"),
            "valid": False,
            "reasons": [],
        }
        eps = 1e-12
        if abs(denominator_dc) <= eps:
            output["reasons"].append("分母在直流处接近零，直流增益无界")
            return output
        output["own_dc_gain_a_per_v"] = (
            own_numerator_dc / denominator_dc)
        output["cross_dc_gain_a_per_v"] = (
            cross_numerator_dc / denominator_dc)
        if abs(own_numerator_dc) <= eps:
            output["reasons"].append("本轴电压直流增益接近零，无法投影R/L")
            return output
        resistance = denominator_dc / own_numerator_dc
        moment = ((b0 + 2.0 * b1) / own_numerator_dc +
                  (a1 + 2.0 * a2 + 3.0 * a3) / denominator_dc)
        inductance_h = resistance * sample_time_s * moment
        output["resistance_ohm"] = resistance
        output["inductance_mh"] = inductance_h * 1e3
        output["normalized_first_moment_samples"] = moment
        if not (resistance > 0.0):
            output["reasons"].append("等效电阻非正，物理投影失败")
        if not (inductance_h > 0.0):
            output["reasons"].append("等效电感非正，物理投影失败")
        output["valid"] = not output["reasons"]
        return output

    d_axis = project(d, 3, 5)
    q_axis = project(q, 5, 3)
    reasons = [
        "七项回归量没有ωe/反电动势、常数项或ESO扰动状态，磁链ψf结构不可辨识",
        "一般双输入ARX(3)包含ESO、延迟和闭环相关性，R/L仅是低频等效投影",
    ]
    for label, axis in (("d轴", d_axis), ("q轴", q_axis)):
        reasons.extend(f"{label}：{item}" for item in axis["reasons"])
    return {
        "verdict": ("diagnostic_only" if d_axis["valid"] and q_axis["valid"]
                    else "invalid_projection"),
        "method": "arx3_dc_gain_and_first_moment",
        "sample_time_s": sample_time_s,
        "d_axis": d_axis,
        "q_axis": q_axis,
        "flux": {
            "identifiable": False,
            "value_wb": None,
            "reason": reasons[0],
        },
        "reasons": reasons,
    }


def cross_check_arx7_motor_projection(
        arx_projection: Mapping[str, object], *,
        physical_iv: Mapping[str, object] | None = None,
        legacy_evidence: Mapping[str, object] | None = None,
        flux_reference_wb: float | None = None,
        resistance_tolerance: float = 0.25,
        inductance_tolerance: float = 0.20) -> dict[str, object]:
    """Compare the ARX equivalent R/L with independent physical evidence."""
    physical_iv = physical_iv or {}
    legacy_evidence = legacy_evidence or {}
    reference: dict[str, float] = {}
    source = "none"
    if physical_iv.get("verdict") == "usable":
        source = "independent_prbs_iv"
        reference = {
            "rd_ohm": float(physical_iv["rd_ohm"]),
            "rq_ohm": float(physical_iv["rq_ohm"]),
            "ld_mh": float(physical_iv["ld_mh"]),
            "lq_mh": float(physical_iv["lq_mh"]),
        }
    elif (legacy_evidence.get("verdict") == "diagnostic_only" and
          "resistance_ohm_median" in legacy_evidence and
          "shared_inductance_mh_median" in legacy_evidence):
        source = "legacy_quasi_steady_voltage_equation"
        resistance = float(legacy_evidence["resistance_ohm_median"])
        inductance = float(legacy_evidence["shared_inductance_mh_median"])
        reference = {
            "rd_ohm": resistance, "rq_ohm": resistance,
            "ld_mh": inductance, "lq_mh": inductance,
        }

    comparisons: dict[str, object] = {}
    matches = []
    for name, r_key, l_key in (
            ("d_axis", "rd_ohm", "ld_mh"),
            ("q_axis", "rq_ohm", "lq_mh")):
        axis = dict(arx_projection.get(name, {}))
        if not axis.get("valid") or not reference:
            comparisons[name] = {
                "comparable": False,
                "reason": ("ARX物理投影无效" if not axis.get("valid")
                           else "没有独立参考结果"),
            }
            matches.append(False)
            continue
        resistance = float(axis["resistance_ohm"])
        inductance = float(axis["inductance_mh"])
        r_ref, l_ref = reference[r_key], reference[l_key]
        r_error = (resistance - r_ref) / r_ref
        l_error = (inductance - l_ref) / l_ref
        matches_axis = (abs(r_error) <= resistance_tolerance and
                        abs(l_error) <= inductance_tolerance)
        comparisons[name] = {
            "comparable": True,
            "resistance_relative_error": r_error,
            "inductance_relative_error": l_error,
            "matches_tolerance": matches_axis,
        }
        matches.append(matches_axis)
    back_emf_absorption: dict[str, object] = {
        "available": False,
        "reason": "缺少q轴ARX投影或速度-电流/磁链诊断",
    }
    q_axis = dict(arx_projection.get("q_axis", {}))
    flux_source = "none"
    if flux_reference_wb is not None:
        try:
            flux = float(flux_reference_wb)
        except (TypeError, ValueError):
            flux = float("nan")
        if math.isfinite(flux) and flux > 0.0:
            flux_source = "independent_motor_reference"
    else:
        flux = float("nan")
    if (not math.isfinite(flux) and "flux_wb_median" in legacy_evidence):
        flux = float(legacy_evidence["flux_wb_median"])
        flux_source = "legacy_voltage_equation_diagnostic"
    if (q_axis.get("valid") and
            "omega_iq_slope_rad_s_per_a_median" in legacy_evidence and
            math.isfinite(flux) and reference):
        slope = float(
            legacy_evidence["omega_iq_slope_rad_s_per_a_median"])
        base_resistance = float(reference["rq_ohm"])
        absorbed_resistance = slope * flux
        predicted_apparent = base_resistance + absorbed_resistance
        observed_apparent = float(q_axis["resistance_ohm"])
        prediction_error = ((observed_apparent - predicted_apparent) /
                            predicted_apparent)
        back_emf_absorption = {
            "available": True,
            "derivation": "Rq_apparent≈Rq+psi_f*d(omega_e)/d(iq)",
            "omega_iq_slope_rad_s_per_a": slope,
            "omega_iq_correlation": float(legacy_evidence.get(
                "omega_iq_correlation_median", float("nan"))),
            "flux_wb": flux,
            "flux_source": flux_source,
            "base_resistance_ohm": base_resistance,
            "absorbed_resistance_ohm": absorbed_resistance,
            "predicted_apparent_resistance_ohm": predicted_apparent,
            "observed_arx_q_resistance_ohm": observed_apparent,
            "relative_prediction_error": prediction_error,
        }
    return {
        "reference_source": source,
        "reference": reference,
        "comparisons": comparisons,
        "rl_match": bool(reference) and all(matches),
        "full_three_parameter_validation": False,
        "back_emf_absorption": back_emf_absorption,
        "verdict": ("rl_consistent_but_flux_unidentifiable"
                    if reference and all(matches)
                    else "not_validated"),
        "reason": "七参数ARX不包含可独立恢复磁链的回归量",
    }


def identify_physical_dq_iv(
        columns: Mapping[str, object], *, pole_pairs: int = 4,
        flux_wb: float = 0.00585,
        nominal_r_ohm: float = 0.59,
        nominal_l_h: float = 0.00066,
        nominal_r_tolerance: float = 0.25,
        nominal_l_tolerance: float = 0.15) -> dict[str, object]:
    """Use probe references as instruments to identify physical dq R/L.

    The controller voltage is endogenous in closed loop: at disturbance
    frequencies it is predominantly the PI's reaction to measured current.
    Therefore ordinary ARX/RLS converges to a biased value.  Current F1/40
    (and legacy F1/32/F1/30) supplies independent d/q PRBS references.  Cross spectra with those references
    form an instrumental-variable estimate of ``U/I``; a discrete first-order
    motor model is then fitted while scanning the actual PWM/sample delay.

    No coefficient clipping or projection is applied.  Physical ranges are
    used only to decide whether the returned estimate is trustworthy.
    """
    count = validate_rls_columns(columns)
    import numpy as np

    required = ("id_a", "idref_a", "sample_seq")
    if (not bool(columns.get("id_source_direct", False)) or
            not bool(columns.get("sequence_source_direct", False)) or
            any(name not in columns for name in required)):
        return {
            "verdict": "probe_required",
            "reasons": [
                "需要F1/40（或兼容F1/32/F1/30）固件直接Id/IdRef和连续样本序号；"
                "旧数据不能用于该物理辨识",
            ],
        }
    rate_hz = int(columns["rate_hz"])
    if rate_hz != 16000:
        return {
            "verdict": "probe_required",
            "reasons": ["真机物理辨识要求16 kHz未平均F1数据"],
        }
    if count < rate_hz * 3:
        return {
            "verdict": "probe_required",
            "reasons": ["辨识激励数据至少需要连续3 s（建议5～10 s）"],
        }

    sequence = np.asarray(columns["sample_seq"], dtype=np.uint16).astype(
        np.uint32)
    sequence_step = (sequence[1:] - sequence[:-1]) & 0xFFFF
    missing_at = np.flatnonzero(sequence_step != 1)
    if missing_at.size:
        first = int(missing_at[0])
        return {
            "verdict": "not_identifiable",
            "reasons": [
                f"16 kHz样本序号在第{first + 1}点不连续；存在固件或主机漏点",
            ],
            "sequence_gap_count": int(missing_at.size),
        }
    tick_ms = np.asarray(columns["tick_ms"], dtype=np.uint32).astype(np.uint64)
    tick_step = (tick_ms[1:] - tick_ms[:-1]) & np.uint64(0xFFFFFFFF)
    tick_gap_at = np.flatnonzero(tick_step > 2)
    if tick_gap_at.size:
        first = int(tick_gap_at[0])
        return {
            "verdict": "not_identifiable",
            "reasons": [
                f"第{first + 1}点前存在{int(tick_step[first])} ms采集停顿；"
                "不能把跨停机/断流样本视为相邻16 kHz数据",
            ],
            "tick_gap_count": int(tick_gap_at.size),
        }

    # F1/40 (and legacy F1/32/F1/30) carries both current-loop feedback Id/Iq and raw phase
    # currents sampled in that same FOC cycle.  The feedback pair may have the
    # optional firmware IIR enabled; identifying the motor from it would fold
    # the filter pole into the estimated inductance.  Rebuild the *raw* Park
    # currents with the exact Park angle instead.  This was unsafe for legacy
    # F1 because its angle was the later reverse-Park compensation angle, hence
    # the explicit id_source_direct/tagged-F1 gate above.
    angle = np.deg2rad(np.asarray(columns["angle_deg"], dtype=float))
    ia = np.asarray(columns["ia_a"], dtype=float)
    ib = np.asarray(columns["ib_a"], dtype=float)
    beta = -(ia + 2.0 * ib) / math.sqrt(3.0)
    id_a = ia * np.sin(angle) + beta * np.cos(angle)
    iq_a = ia * np.cos(angle) - beta * np.sin(angle)
    feedback_id = np.asarray(columns["id_a"], dtype=float)
    feedback_iq = np.asarray(columns["iq_a"], dtype=float)
    id_ref = np.asarray(columns["idref_a"], dtype=float)
    iq_ref = np.asarray(columns["iqref_a"], dtype=float)
    speed_rpm = np.asarray(columns["speed_rpm"], dtype=float)
    vbus_v = np.asarray(columns["vbus_v"], dtype=float)
    finite_vbus = vbus_v[np.isfinite(vbus_v)]
    median_vbus_v = (float(np.median(finite_vbus))
                     if finite_vbus.size else float("nan"))
    median_vdda_v = float("nan")
    if bool(columns.get("vdda_source_direct", False)) and "vdda_v" in columns:
        vdda_values = np.asarray(columns["vdda_v"], dtype=float)
        finite_vdda = vdda_values[np.isfinite(vdda_values)]
        if finite_vdda.size:
            median_vdda_v = float(np.median(finite_vdda))
    if not (5.0 <= median_vbus_v <= 80.0):
        return {
            "verdict": "not_identifiable",
            "reasons": ["母线电压原始ADC换算无效，不能建立可信电压尺度"],
        }
    ud_v, uq_v, voltage_source = resolve_voltage_columns_si(columns)
    # Use the exact Park angle carried by every tagged F1 sample to establish both
    # the electrical speed and its sign.  A cached mechanical-speed field can
    # be one slow-task old, and encoder installation conventions can invert its
    # sign relative to the electrical angle.  A centred ~16 ms moving average
    # removes encoder quantisation without adding causal phase delay offline.
    unwrapped_angle = np.unwrap(angle)
    omega_e_raw = np.gradient(unwrapped_angle) * rate_hz
    omega_window = min(257, count if count % 2 else count - 1)
    if omega_window >= 3:
        pad = omega_window // 2
        omega_e = np.convolve(
            np.pad(omega_e_raw, (pad, pad), mode="edge"),
            np.ones(omega_window, dtype=float) / omega_window,
            mode="valid")
    else:
        omega_e = omega_e_raw
    omega_e_from_speed = (speed_rpm * max(1, int(pole_pairs)) *
                          2.0 * math.pi / 60.0)
    median_electrical_hz = float(np.median(omega_e)) / (2.0 * math.pi)
    median_speed_electrical_hz = (float(np.median(omega_e_from_speed)) /
                                  (2.0 * math.pi))
    median_speed_rpm = float(np.median(speed_rpm))
    speed_p05_rpm, speed_p95_rpm = (
        float(value) for value in np.percentile(speed_rpm, (5.0, 95.0)))
    speed_p90_span_rpm = speed_p95_rpm - speed_p05_rpm
    id_ref_ac = id_ref - np.mean(id_ref)
    iq_ref_ac = iq_ref - np.mean(iq_ref)
    reference_norm = float(np.linalg.norm(id_ref_ac) *
                           np.linalg.norm(iq_ref_ac))
    probe_reference_correlation = (
        float(np.dot(id_ref_ac, iq_ref_ac) / reference_norm)
        if reference_norm > 1e-15 else float("nan"))

    # 4096 points still gives 3.90625 Hz resolution at 16 kHz, while yielding
    # roughly twice as many independent Welch averages as 8192 points.  The
    # extra averaging is materially more robust to the phase-current noise
    # seen on hardware; the lost sub-4-Hz resolution is irrelevant to the
    # 80..1200 Hz motor-impedance fit band.
    max_fft = min(4096, count // 4)
    nfft = 1 << int(math.floor(math.log2(max_fft)))
    if nfft < 2048:
        return {
            "verdict": "probe_required",
            "reasons": ["有效连续数据太短，无法形成稳定的工具变量频谱"],
        }

    def axis_fit(current, voltage, reference) -> dict[str, object]:
        hop = nfft // 2
        window = np.hanning(nfft)
        bins = nfft // 2 + 1

        def empty_spectra():
            return {
                "rr": np.zeros(bins, dtype=complex),
                "ir": np.zeros(bins, dtype=complex),
                "ur": np.zeros(bins, dtype=complex),
                "ii": np.zeros(bins, dtype=complex),
                "segments": 0,
                "ref_std": [],
            }

        spectra = [empty_spectra(), empty_spectra(), empty_spectra()]
        start_sample = min(rate_hz // 2, max(0, count - nfft))
        for start in range(start_sample, count - nfft + 1, hop):
            stop = start + nfft
            r = reference[start:stop] - np.mean(reference[start:stop])
            i = current[start:stop] - np.mean(current[start:stop])
            u = voltage[start:stop] - np.mean(voltage[start:stop])
            rf = np.fft.rfft(r * window)
            inf = np.fft.rfft(i * window)
            uf = np.fft.rfft(u * window)
            # Keep one full Welch estimate plus genuinely early/late temporal
            # subsets.  Alternating odd/even windows only checks numerical
            # repeatability; it cannot reveal thermal drift or a late loss of
            # excitation in a real motor run.
            half_index = 1 if (start + stop) // 2 < count // 2 else 2
            for index in (0, half_index):
                target = spectra[index]
                target["rr"] += rf * np.conj(rf)
                target["ir"] += inf * np.conj(rf)
                target["ur"] += uf * np.conj(rf)
                target["ii"] += inf * np.conj(inf)
                target["segments"] += 1
                target["ref_std"].append(float(np.std(r)))

        frequencies = np.fft.rfftfreq(nfft, 1.0 / rate_hz)

        def fit_spectra(spec, forced_delay=None):
            if spec["segments"] < 2:
                return None
            rr = spec["rr"].real
            ir = spec["ir"]
            ur = spec["ur"]
            ii = spec["ii"].real
            band = ((frequencies >= 80.0) &
                    (frequencies <= min(1200.0, rate_hz * 0.35)))
            if not np.any(band):
                return None
            energy_floor = np.percentile(rr[band], 50.0)
            coherence = np.abs(ir) ** 2 / np.maximum(rr * ii, 1e-30)
            selected = (band & (rr >= energy_floor) &
                        (coherence >= 0.10) & (np.abs(ir) > 1e-12))
            if np.count_nonzero(selected) < 20:
                return None
            z_iv = ur[selected] / ir[selected]
            omega = 2.0 * math.pi * frequencies[selected] / rate_hz
            weights = np.clip(coherence[selected], 0.05, 1.0)
            sqrt_w = np.sqrt(weights)
            # Centre-aligned PWM plus ADC/compute/update timing is commonly a
            # fractional (about 1.5-cycle) delay.  Restricting the scan to
            # integers makes that phase error look like extra inductance.
            delays = ((float(forced_delay),) if forced_delay is not None else
                      np.arange(0.0, 4.0001, 0.125))
            best = None
            for delay in delays:
                design = np.column_stack((
                    np.exp(1j * omega * delay),
                    np.exp(1j * omega * (delay - 1)),
                ))
                real_design = np.vstack((
                    design.real * sqrt_w[:, None],
                    design.imag * sqrt_w[:, None],
                ))
                real_target = np.concatenate((
                    z_iv.real * sqrt_w, z_iv.imag * sqrt_w))
                coeff, _, _, _ = np.linalg.lstsq(
                    real_design, real_target, rcond=None)
                predicted = design @ coeff
                normalized_error = math.sqrt(float(np.sum(
                    weights * np.abs(z_iv - predicted) ** 2) /
                    max(np.sum(weights * np.abs(z_iv) ** 2), 1e-30)))
                c0, c1 = (float(coeff[0]), float(coeff[1]))
                if abs(c0) < 1e-15:
                    continue
                a = -c1 / c0
                b = 1.0 / c0
                candidate = {
                    "delay_samples": float(delay), "fit_nrmse": normalized_error,
                    "a": a, "b_a_per_v": b,
                    "selected_bins": int(np.count_nonzero(selected)),
                    "selected_frequency_hz": (
                        float(frequencies[selected][0]),
                        float(frequencies[selected][-1])),
                    "median_coherence": float(np.median(coherence[selected])),
                    "reference_ripple_std_a": float(np.median(spec["ref_std"])),
                }
                if 0.0 < a < 1.0 and b > 0.0:
                    resistance = (1.0 - a) / b
                    inductance = (-resistance / rate_hz / math.log(a))
                    candidate.update({
                        "resistance_ohm": resistance,
                        "inductance_mh": inductance * 1e3,
                    })
                else:
                    candidate.update({
                        "resistance_ohm": float("nan"),
                        "inductance_mh": float("nan"),
                    })
                if best is None or normalized_error < best["fit_nrmse"]:
                    best = candidate
            return best

        full = fit_spectra(spectra[0])
        if full is None:
            return {
                "verdict": "probe_required",
                "reasons": ["参考激励与电流之间缺少足够的工具变量相干频点"],
            }
        halves = [fit_spectra(spec, full["delay_samples"])
                  for spec in spectra[1:]]
        reasons = []
        resistance = float(full["resistance_ohm"])
        inductance_mh = float(full["inductance_mh"])
        if float(full["reference_ripple_std_a"]) < 0.015:
            reasons.append("参考量高频激励不足（请启用辨识激励）")
        if not (0.1 <= resistance <= 2.0 and 0.1 <= inductance_mh <= 3.0):
            reasons.append("离散极点/输入增益未给出可信的正R/L")
        if float(full["fit_nrmse"]) > 0.25:
            reasons.append(
                f"工具变量阻抗拟合残差过大({full['fit_nrmse']:.3f})")
        split_values = []
        if all(item is not None for item in halves):
            split_values = [(float(item["resistance_ohm"]),
                             float(item["inductance_mh"]))
                            for item in halves]
            for name, value, index in (("R", resistance, 0),
                                       ("L", inductance_mh, 1)):
                spread = abs(split_values[0][index] - split_values[1][index])
                if not math.isfinite(value) or spread > 0.35 * max(abs(value), 1e-9):
                    reasons.append(f"前后分段{name}估计不一致")
        else:
            reasons.append("前后分段中至少一段缺少足够激励，不能验证重复性")
        full["split_estimates"] = split_values
        full["verdict"] = "usable" if not reasons else "not_identifiable"
        full["reasons"] = reasons
        return full

    # Cross-coupling compensation is iterated; it changes the input signal,
    # not the estimator output constraints.
    ld_h = lq_h = float(nominal_l_h)
    d_result = q_result = None
    for _ in range(2):
        d_voltage = ud_v + omega_e * lq_h * iq_a
        q_voltage = uq_v - omega_e * (float(flux_wb) + ld_h * id_a)
        d_result = axis_fit(id_a, d_voltage, id_ref)
        q_result = axis_fit(iq_a, q_voltage, iq_ref)
        if d_result.get("verdict") == "usable":
            ld_h = float(d_result["inductance_mh"]) * 1e-3
        if q_result.get("verdict") == "usable":
            lq_h = float(q_result["inductance_mh"]) * 1e-3

    reasons = [f"d轴：{reason}" for reason in d_result.get("reasons", ())]
    reasons += [f"q轴：{reason}" for reason in q_result.get("reasons", ())]
    allowed_speed_span_rpm = max(40.0, 0.15 * abs(median_speed_rpm))
    if speed_p90_span_rpm > allowed_speed_span_rpm:
        reasons.append(
            f"采集期间转速未稳态(P5～P95跨度{speed_p90_span_rpm:.1f} rpm)")
    frequency_mismatch_hz = abs(
        median_electrical_hz - median_speed_electrical_hz)
    allowed_frequency_mismatch_hz = max(
        3.0, 0.10 * max(abs(median_electrical_hz),
                        abs(median_speed_electrical_hz)))
    if frequency_mismatch_hz > allowed_frequency_mismatch_hz:
        reasons.append(
            "Park角电频率与转速×极对数不一致"
            f"({median_electrical_hz:.2f}/{median_speed_electrical_hz:.2f} Hz)")
    if (math.isfinite(probe_reference_correlation) and
            abs(probe_reference_correlation) > 0.35):
        reasons.append(
            f"d/q辨识激励相关性过高({probe_reference_correlation:.3f})")
    usable = (d_result.get("verdict") == "usable" and
              q_result.get("verdict") == "usable" and not reasons)
    physical_values = {
        "rd_ohm": float(d_result.get("resistance_ohm", float("nan"))),
        "rq_ohm": float(q_result.get("resistance_ohm", float("nan"))),
        "ld_mh": float(d_result.get("inductance_mh", float("nan"))),
        "lq_mh": float(q_result.get("inductance_mh", float("nan"))),
    }
    nominal_values = {
        "rd_ohm": float(nominal_r_ohm),
        "rq_ohm": float(nominal_r_ohm),
        "ld_mh": float(nominal_l_h) * 1e3,
        "lq_mh": float(nominal_l_h) * 1e3,
    }
    nominal_relative_error = {
        name: abs(physical_values[name] - target) / max(abs(target), 1e-15)
        for name, target in nominal_values.items()
    }
    nominal_reasons = []
    for name in ("rd_ohm", "rq_ohm"):
        if (not math.isfinite(nominal_relative_error[name]) or
                nominal_relative_error[name] > nominal_r_tolerance):
            nominal_reasons.append(
                f"{name}偏离标称{nominal_relative_error[name] * 100:.1f}%")
    for name in ("ld_mh", "lq_mh"):
        if (not math.isfinite(nominal_relative_error[name]) or
                nominal_relative_error[name] > nominal_l_tolerance):
            nominal_reasons.append(
                f"{name}偏离标称{nominal_relative_error[name] * 100:.1f}%")
    nominal_match = bool(usable and not nominal_reasons)
    return {
        "verdict": "usable" if usable else "not_identifiable",
        "reasons": reasons,
        "method": "closed_loop_frequency_iv",
        "rate_hz": rate_hz,
        "sample_count": count,
        "flux_wb": float(flux_wb),
        "median_vbus_v": median_vbus_v,
        "median_vdda_v": median_vdda_v,
        "voltage_source": voltage_source,
        "electrical_frequency_hz_from_angle": median_electrical_hz,
        "electrical_frequency_hz_from_speed": median_speed_electrical_hz,
        "median_speed_rpm": median_speed_rpm,
        "speed_p90_span_rpm": speed_p90_span_rpm,
        "probe_reference_correlation": probe_reference_correlation,
        "feedback_vs_raw_id_rmse_a": float(np.sqrt(np.mean(
            (feedback_id - id_a) ** 2))),
        "feedback_vs_raw_iq_rmse_a": float(np.sqrt(np.mean(
            (feedback_iq - iq_a) ** 2))),
        "d_axis": d_result,
        "q_axis": q_result,
        **physical_values,
        "nominal_r_ohm": float(nominal_r_ohm),
        "nominal_l_mh": float(nominal_l_h) * 1e3,
        "nominal_match": nominal_match,
        "nominal_reasons": nominal_reasons,
        "nominal_relative_error": nominal_relative_error,
    }
