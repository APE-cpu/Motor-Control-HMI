"""修正 2026-09-29 之前保存的高速数据里错解的母线电压。

旧版上位机把 F1 帧的母线 ADC 按 MCSDK 生成的 0.027 接地分压解码：
    Vbus_old = adc/65536 × VDDA / 0.027
而野火骄阳板固件实际用的是带 1.65 V 偏置、放大 37 倍的换算（见 config.f1_bus_voltage），
因此旧文件里的 vbus_v 约为真实值的 3.6 倍（24 V 电源记成约 84 V），由 vbus 推出的
vd_applied_v / vq_applied_v 也同比偏大。

按正确公式，ADC 满量程对应的母线电压上限为 (3.3 − 1.65) × 37 ≈ 61 V，
所以 vbus_v 中位数超过这个上限的文件一定是旧解码，可以精确还原 ADC 读数后重算。
"""
from __future__ import annotations

import numpy as np

from config.config import (
    F1_VBUS_ADC_REF_V, F1_VBUS_BIAS_V, F1_VBUS_GAIN,
)

LEGACY_PARTITION_FACTOR = 0.0270
MAX_DECODABLE_V = (F1_VBUS_ADC_REF_V - F1_VBUS_BIAS_V) * F1_VBUS_GAIN


def is_legacy_vbus(vbus_v) -> bool:
    values = np.asarray(vbus_v, dtype=float)
    finite = values[np.isfinite(values)]
    return bool(finite.size) and float(np.median(finite)) > MAX_DECODABLE_V + 0.5


def legacy_vbus_ratio(vbus_v, vdda_v=None) -> np.ndarray | None:
    """旧文件返回逐点比例（正确 / 旧值），新文件返回 None。"""
    vbus = np.asarray(vbus_v, dtype=float)
    if not is_legacy_vbus(vbus):
        return None
    vdda = (np.asarray(vdda_v, dtype=float) if vdda_v is not None
            else np.full(vbus.shape, F1_VBUS_ADC_REF_V))
    vdda = np.where(np.isfinite(vdda) & (vdda > 2.0), vdda, F1_VBUS_ADC_REF_V)
    adc_fraction = vbus * LEGACY_PARTITION_FACTOR / vdda
    fixed = (adc_fraction * F1_VBUS_ADC_REF_V - F1_VBUS_BIAS_V) * F1_VBUS_GAIN
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(np.abs(vbus) > 1e-9, fixed / vbus, 1.0)
    return ratio


def correct_columns(columns: dict) -> bool:
    """原地修正列字典里的 vbus_v 与 vd/vq_applied_v；返回是否做了修正。"""
    if "vbus_v" not in columns:
        return False
    vbus = np.asarray(columns["vbus_v"], dtype=float)
    ratio = legacy_vbus_ratio(vbus, columns.get("vdda_v"))
    if ratio is None:
        return False
    columns["vbus_v"] = vbus * ratio
    for name in ("vd_applied_v", "vq_applied_v"):
        if name in columns:
            columns[name] = np.asarray(columns[name], dtype=float) * ratio
    columns["vbus_legacy_corrected"] = True
    return True
