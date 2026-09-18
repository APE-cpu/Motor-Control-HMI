"""控制环传递函数、理论波特图与运行数据频响估计。"""
from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHeaderView, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

try:
    import numpy as np
    import pyqtgraph as pg
    _PLOT_OK = True
except Exception:  # pragma: no cover - 依赖缺失时页面仍能打开
    _PLOT_OK = False


def _crossing_log_frequency(frequency, values, target=0.0):
    """在对数频率轴上线性插值第一个穿越点。"""
    for index in range(len(frequency) - 1):
        y0 = float(values[index] - target)
        y1 = float(values[index + 1] - target)
        if y0 == 0.0:
            return float(frequency[index]), float(values[index])
        if y0 * y1 <= 0.0 and y0 != y1:
            ratio = -y0 / (y1 - y0)
            log_f = (math.log10(float(frequency[index])) + ratio *
                     (math.log10(float(frequency[index + 1])) -
                      math.log10(float(frequency[index]))))
            value = float(values[index] + ratio *
                          (values[index + 1] - values[index]))
            return 10.0 ** log_f, value
    return float("nan"), float("nan")


def _poly_add_descending(left, right):
    """相加两个按降幂排列的多项式系数。"""
    left = np.asarray(left, dtype=float)
    right = np.asarray(right, dtype=float)
    size = max(left.size, right.size)
    return np.pad(left, (size - left.size, 0)) + np.pad(
        right, (size - right.size, 0))


def _roots_descending(coefficients):
    coefficients = np.asarray(coefficients, dtype=float)
    nonzero = np.flatnonzero(np.abs(coefficients) > 1e-12)
    if nonzero.size == 0:
        return np.asarray([], dtype=complex)
    coefficients = coefficients[nonzero[0]:]
    if coefficients.size <= 1:
        return np.asarray([], dtype=complex)
    return np.roots(coefficients)


def _closed_loop_roots_continuous(kp, ki, resistance, inductance,
                                  delay_s, filter_cutoff_hz):
    """用一阶Padé延迟求连续模型的闭环零极点。"""
    controller_num = np.asarray([kp, ki], dtype=float)
    controller_den = np.asarray([1.0, 0.0], dtype=float)
    plant_num = np.asarray([1.0], dtype=float)
    plant_den = np.asarray([inductance, resistance], dtype=float)
    if delay_s > 0.0:
        delay_num = np.asarray([-delay_s / 2.0, 1.0], dtype=float)
        delay_den = np.asarray([delay_s / 2.0, 1.0], dtype=float)
        delay_note = "计算/PWM纯延迟采用一阶 Padé 近似"
    else:
        delay_num = delay_den = np.asarray([1.0], dtype=float)
        delay_note = "无纯延迟近似"
    if filter_cutoff_hz is not None and filter_cutoff_hz > 0.0:
        omega_f = 2.0 * np.pi * filter_cutoff_hz
        filter_num = np.asarray([omega_f], dtype=float)
        filter_den = np.asarray([1.0, omega_f], dtype=float)
    else:
        filter_num = filter_den = np.asarray([1.0], dtype=float)
    forward_num = np.convolve(np.convolve(controller_num, plant_num), delay_num)
    forward_den = np.convolve(np.convolve(controller_den, plant_den), delay_den)
    characteristic = _poly_add_descending(
        np.convolve(forward_den, filter_den),
        np.convolve(forward_num, filter_num))
    closed_num = np.convolve(forward_num, filter_den)
    return (_roots_descending(characteristic),
            _roots_descending(closed_num), delay_note)


def _closed_loop_roots_discrete(controller_num_q, controller_den_q,
                                plant_num_q, plant_den_q,
                                extra_delay_cycles, filter_num_q,
                                filter_den_q):
    """求q=z^-1多项式闭环零极点；分数延迟用一阶Thiran近似。"""
    integer_delay = int(math.floor(extra_delay_cycles + 1e-12))
    fractional_delay = float(extra_delay_cycles) - integer_delay
    delay_num_q = np.asarray([0.0] * integer_delay + [1.0])
    delay_den_q = np.asarray([1.0])
    if fractional_delay > 1e-9:
        thiran_a = (1.0 - fractional_delay) / (1.0 + fractional_delay)
        delay_num_q = np.convolve(delay_num_q, [thiran_a, 1.0])
        delay_den_q = np.asarray([1.0, thiran_a])
        delay_note = (
            f"额外{extra_delay_cycles:.3g}周期延迟：{integer_delay}拍整数延迟 + "
            f"一阶 Thiran 分数延迟（D={fractional_delay:.3g}）")
    else:
        delay_note = f"额外{integer_delay}拍整数延迟（无分数延迟近似）"

    forward_num_q = np.convolve(
        np.convolve(controller_num_q, plant_num_q), delay_num_q)
    forward_den_q = np.convolve(
        np.convolve(controller_den_q, plant_den_q), delay_den_q)
    left_q = np.convolve(forward_den_q, filter_den_q)
    right_q = np.convolve(forward_num_q, filter_num_q)
    degree = max(left_q.size, right_q.size)
    characteristic_q = np.pad(left_q, (0, degree - left_q.size)) + np.pad(
        right_q, (0, degree - right_q.size))
    closed_num_q = np.convolve(forward_num_q, filter_den_q)
    closed_num_z = np.pad(
        closed_num_q, (0, max(0, characteristic_q.size - closed_num_q.size)))
    # q的升幂系数恰好是变换到z平面后的降幂系数。
    return (_roots_descending(characteristic_q),
            _roots_descending(closed_num_z), delay_note)


def current_loop_frequency_response(
        resistance_ohm: float, inductance_h: float,
        kp: float, ki: float, delay_s: float,
        filter_cutoff_hz: float | None = None,
        min_hz: float = 1.0, max_hz: float = 8000.0,
        points: int = 1600) -> dict:
    """计算简化q轴电流环的理论频响，不依赖SciPy。"""
    if not _PLOT_OK:
        raise RuntimeError("未安装 numpy/pyqtgraph")
    r = float(resistance_ohm)
    l = float(inductance_h)
    kp = float(kp)
    ki = float(ki)
    delay = float(delay_s)
    if not (r > 0.0 and l > 0.0 and kp >= 0.0 and ki >= 0.0 and
            delay >= 0.0 and 0.0 < min_hz < max_hz and points >= 64):
        raise ValueError("模型参数或频率范围无效")

    frequency = np.logspace(math.log10(min_hz), math.log10(max_hz), points)
    omega = 2.0 * np.pi * frequency
    s = 1j * omega
    plant = 1.0 / (l * s + r)
    controller = kp + ki / s
    delay_tf = np.exp(-s * delay)
    feedback_filter = np.ones_like(s, dtype=complex)
    if filter_cutoff_hz is not None and float(filter_cutoff_hz) > 0.0:
        omega_f = 2.0 * np.pi * float(filter_cutoff_hz)
        feedback_filter = omega_f / (s + omega_f)

    forward = controller * plant * delay_tf
    loop = forward * feedback_filter
    closed = forward / (1.0 + loop)
    sensitivity = 1.0 / (1.0 + loop)
    complementary = loop / (1.0 + loop)
    control_response = controller / (1.0 + loop)
    magnitude_plant_db = 20.0 * np.log10(np.maximum(np.abs(plant), 1e-15))
    magnitude_loop_db = 20.0 * np.log10(np.maximum(np.abs(loop), 1e-15))
    magnitude_closed_db = 20.0 * np.log10(np.maximum(np.abs(closed), 1e-15))
    phase_loop_deg = np.unwrap(np.angle(loop)) * 180.0 / np.pi
    phase_closed_deg = np.unwrap(np.angle(closed)) * 180.0 / np.pi

    gain_cross_hz, _ = _crossing_log_frequency(
        frequency, magnitude_loop_db, 0.0)
    if math.isfinite(gain_cross_hz):
        phase_at_gain_cross = float(np.interp(
            math.log10(gain_cross_hz), np.log10(frequency), phase_loop_deg))
        phase_margin_deg = 180.0 + phase_at_gain_cross
    else:
        phase_at_gain_cross = phase_margin_deg = float("nan")

    phase_cross_hz, _ = _crossing_log_frequency(
        frequency, phase_loop_deg, -180.0)
    if math.isfinite(phase_cross_hz):
        magnitude_at_phase_cross = float(np.interp(
            math.log10(phase_cross_hz), np.log10(frequency),
            magnitude_loop_db))
        gain_margin_db = -magnitude_at_phase_cross
    else:
        gain_margin_db = float("inf")

    low_frequency_gain_db = float(magnitude_closed_db[0])
    closed_bandwidth_hz, _ = _crossing_log_frequency(
        frequency, magnitude_closed_db, low_frequency_gain_db - 3.0)
    resonance_index = int(np.argmax(magnitude_closed_db))
    nyquist_distance = np.abs(loop + 1.0)
    nyquist_nearest_index = int(np.argmin(nyquist_distance))
    sensitivity_abs = np.abs(sensitivity)
    complementary_abs = np.abs(complementary)
    ms_index = int(np.argmax(sensitivity_abs))
    mt_index = int(np.argmax(complementary_abs))
    poles, zeros, pole_model_note = _closed_loop_roots_continuous(
        kp, ki, r, l, delay,
        float(filter_cutoff_hz) if filter_cutoff_hz is not None else None)
    return {
        "frequency_hz": frequency,
        "plant_db": magnitude_plant_db,
        "loop_db": magnitude_loop_db,
        "closed_db": magnitude_closed_db,
        "loop_phase_deg": phase_loop_deg,
        "closed_phase_deg": phase_closed_deg,
        "gain_cross_hz": gain_cross_hz,
        "phase_at_gain_cross_deg": phase_at_gain_cross,
        "phase_margin_deg": phase_margin_deg,
        "phase_cross_hz": phase_cross_hz,
        "gain_margin_db": gain_margin_db,
        "closed_bandwidth_hz": closed_bandwidth_hz,
        "resonance_hz": float(frequency[resonance_index]),
        "resonance_db": float(magnitude_closed_db[resonance_index]),
        "loop_complex": loop,
        "nyquist_min_distance": float(nyquist_distance[nyquist_nearest_index]),
        "nyquist_min_distance_hz": float(frequency[nyquist_nearest_index]),
        "poles": poles,
        "zeros": zeros,
        "pole_domain": "s",
        "pole_model_note": pole_model_note,
        "sensitivity_db": 20.0 * np.log10(np.maximum(sensitivity_abs, 1e-15)),
        "complementary_db": 20.0 * np.log10(np.maximum(complementary_abs, 1e-15)),
        "control_response_db": 20.0 * np.log10(
            np.maximum(np.abs(control_response), 1e-15)),
        "ms": float(sensitivity_abs[ms_index]),
        "ms_frequency_hz": float(frequency[ms_index]),
        "mt": float(complementary_abs[mt_index]),
        "mt_frequency_hz": float(frequency[mt_index]),
    }


def firmware_current_loop_frequency_response(
        resistance_ohm: float, inductance_h: float,
        sample_rate_hz: float, bus_voltage_v: float,
        adc_reference_v: float, shunt_ohm: float, amplifier_gain: float,
        kp_digit: int, ki_digit: int, kp_divisor: int, ki_divisor: int,
        extra_delay_cycles: float = 0.5,
        filter_enabled: bool = False, filter_alpha_q15: int = 20491,
        min_hz: float = 1.0, max_hz: float = 8000.0,
        points: int = 1600) -> dict:
    """复现MCSDK定点PI、digit比例、ZOH对象及反馈IIR的离散频响。"""
    if not _PLOT_OK:
        raise RuntimeError("未安装 numpy/pyqtgraph")
    r = float(resistance_ohm)
    l = float(inductance_h)
    rate = float(sample_rate_hz)
    vbus = float(bus_voltage_v)
    vref = float(adc_reference_v)
    rshunt = float(shunt_ohm)
    gain = float(amplifier_gain)
    kp_div = int(kp_divisor)
    ki_div = int(ki_divisor)
    alpha_q15 = int(filter_alpha_q15)
    if not (r > 0.0 and l > 0.0 and rate > 0.0 and vbus > 0.0 and
            vref > 0.0 and rshunt > 0.0 and gain > 0.0 and
            kp_div > 0 and ki_div > 0 and float(extra_delay_cycles) >= 0.0):
        raise ValueError("固件离散模型参数无效")
    if filter_enabled and not 0 < alpha_q15 < 32768:
        raise ValueError("反馈IIR的alpha_q15必须在1..32767")
    nyquist_limit = rate * 0.49
    upper = min(float(max_hz), nyquist_limit)
    if not 0.0 < float(min_hz) < upper:
        raise ValueError("频率范围超过离散系统Nyquist限制")

    frequency = np.logspace(math.log10(min_hz), math.log10(upper), points)
    omega = 2.0 * np.pi * frequency
    sample_time = 1.0 / rate
    z_inv = np.exp(-1j * omega * sample_time)

    # ADC/Park电流仍沿用MCSDK的16位digit量纲；SVPWM电压digit按固件RLS
    # 路径中的同一换算：Vbus/(sqrt(3)*32768)。
    current_digit_per_amp = 65536.0 * rshunt * gain / vref
    volt_per_digit = vbus / (math.sqrt(3.0) * 32768.0)
    digit_loop_scale = current_digit_per_amp * volt_per_digit
    kp_physical = digit_loop_scale * int(kp_digit) / kp_div
    ki_step_physical = digit_loop_scale * int(ki_digit) / ki_div
    ki_continuous_equivalent = ki_step_physical * rate

    # PI_Controller先累加当前误差，再输出，因此积分支路是
    # (Ki_digit/KiDiv)/(1-z^-1)，而不是连续域Ki/s。
    controller = (kp_physical +
                  ki_step_physical / (1.0 - z_inv))
    plant_pole = math.exp(-r * sample_time / l)
    plant_gain = (1.0 - plant_pole) / r
    # y[k+1]=a*y[k]+b*u[k]：z^-1包含ZOH对象固有的一拍更新。
    plant = plant_gain * z_inv / (1.0 - plant_pole * z_inv)
    extra_delay = np.exp(
        -1j * omega * sample_time * float(extra_delay_cycles))
    feedback_filter = np.ones_like(z_inv, dtype=complex)
    alpha = 1.0
    if filter_enabled:
        alpha = alpha_q15 / 32768.0
        feedback_filter = alpha / (1.0 - (1.0 - alpha) * z_inv)

    forward = controller * plant * extra_delay
    loop = forward * feedback_filter
    closed_raw = forward / (1.0 + loop)
    closed_feedback = closed_raw * feedback_filter
    sensitivity = 1.0 / (1.0 + loop)
    complementary = loop / (1.0 + loop)
    control_response = controller / (1.0 + loop)
    plant_db = 20.0 * np.log10(np.maximum(np.abs(plant), 1e-15))
    loop_db = 20.0 * np.log10(np.maximum(np.abs(loop), 1e-15))
    closed_db = 20.0 * np.log10(np.maximum(np.abs(closed_raw), 1e-15))
    feedback_db = 20.0 * np.log10(
        np.maximum(np.abs(closed_feedback), 1e-15))
    loop_phase = np.unwrap(np.angle(loop)) * 180.0 / np.pi
    closed_phase = np.unwrap(np.angle(closed_raw)) * 180.0 / np.pi

    gain_cross_hz, _ = _crossing_log_frequency(frequency, loop_db, 0.0)
    if math.isfinite(gain_cross_hz):
        phase_at_gain_cross = float(np.interp(
            math.log10(gain_cross_hz), np.log10(frequency), loop_phase))
        phase_margin_deg = 180.0 + phase_at_gain_cross
    else:
        phase_at_gain_cross = phase_margin_deg = float("nan")
    phase_cross_hz, _ = _crossing_log_frequency(frequency, loop_phase, -180.0)
    if math.isfinite(phase_cross_hz):
        magnitude_at_phase_cross = float(np.interp(
            math.log10(phase_cross_hz), np.log10(frequency), loop_db))
        gain_margin_db = -magnitude_at_phase_cross
    else:
        gain_margin_db = float("inf")
    closed_bandwidth_hz, _ = _crossing_log_frequency(
        frequency, closed_db, float(closed_db[0]) - 3.0)
    resonance_index = int(np.argmax(closed_db))
    feedback_bandwidth_hz, _ = _crossing_log_frequency(
        frequency, feedback_db, float(feedback_db[0]) - 3.0)
    feedback_resonance_index = int(np.argmax(feedback_db))
    nyquist_distance = np.abs(loop + 1.0)
    nyquist_nearest_index = int(np.argmin(nyquist_distance))
    sensitivity_abs = np.abs(sensitivity)
    complementary_abs = np.abs(complementary)
    ms_index = int(np.argmax(sensitivity_abs))
    mt_index = int(np.argmax(complementary_abs))
    filter_cutoff_hz = (
        -rate / (2.0 * math.pi) * math.log(1.0 - alpha)
        if filter_enabled and alpha < 1.0 else float("inf"))
    controller_num_q = np.asarray(
        [kp_physical + ki_step_physical, -kp_physical])
    controller_den_q = np.asarray([1.0, -1.0])
    plant_num_q = np.asarray([0.0, plant_gain])
    plant_den_q = np.asarray([1.0, -plant_pole])
    if filter_enabled:
        filter_num_q = np.asarray([alpha])
        filter_den_q = np.asarray([1.0, -(1.0 - alpha)])
    else:
        filter_num_q = filter_den_q = np.asarray([1.0])
    poles, zeros, pole_model_note = _closed_loop_roots_discrete(
        controller_num_q, controller_den_q, plant_num_q, plant_den_q,
        float(extra_delay_cycles), filter_num_q, filter_den_q)
    return {
        "frequency_hz": frequency,
        "plant_db": plant_db,
        "loop_db": loop_db,
        "closed_db": closed_db,
        "feedback_db": feedback_db,
        "loop_phase_deg": loop_phase,
        "closed_phase_deg": closed_phase,
        "gain_cross_hz": gain_cross_hz,
        "phase_at_gain_cross_deg": phase_at_gain_cross,
        "phase_margin_deg": phase_margin_deg,
        "phase_cross_hz": phase_cross_hz,
        "gain_margin_db": gain_margin_db,
        "closed_bandwidth_hz": closed_bandwidth_hz,
        "feedback_bandwidth_hz": feedback_bandwidth_hz,
        "resonance_hz": float(frequency[resonance_index]),
        "resonance_db": float(closed_db[resonance_index]),
        "feedback_resonance_hz": float(frequency[feedback_resonance_index]),
        "feedback_resonance_db": float(feedback_db[feedback_resonance_index]),
        "loop_complex": loop,
        "nyquist_min_distance": float(nyquist_distance[nyquist_nearest_index]),
        "nyquist_min_distance_hz": float(frequency[nyquist_nearest_index]),
        "poles": poles,
        "zeros": zeros,
        "pole_domain": "z",
        "pole_model_note": pole_model_note,
        "sensitivity_db": 20.0 * np.log10(np.maximum(sensitivity_abs, 1e-15)),
        "complementary_db": 20.0 * np.log10(np.maximum(complementary_abs, 1e-15)),
        "control_response_db": 20.0 * np.log10(
            np.maximum(np.abs(control_response), 1e-15)),
        "ms": float(sensitivity_abs[ms_index]),
        "ms_frequency_hz": float(frequency[ms_index]),
        "mt": float(complementary_abs[mt_index]),
        "mt_frequency_hz": float(frequency[mt_index]),
        "current_digit_per_amp": current_digit_per_amp,
        "amp_per_current_digit": 1.0 / current_digit_per_amp,
        "volt_per_voltage_digit": volt_per_digit,
        "kp_physical": kp_physical,
        "ki_step_physical": ki_step_physical,
        "ki_continuous_equivalent": ki_continuous_equivalent,
        "plant_pole_z": plant_pole,
        "filter_alpha": alpha,
        "filter_cutoff_hz": filter_cutoff_hz,
        "total_delay_cycles": 1.0 + float(extra_delay_cycles),
    }


def estimate_welch_frf(input_values, output_values, sample_rate_hz: float,
                       segment_length: int = 1024) -> dict:
    """用Welch H1估计器计算 y/x 闭环频响及相干度。"""
    if not _PLOT_OK:
        raise RuntimeError("未安装 numpy/pyqtgraph")
    rate = float(sample_rate_hz)
    if not math.isfinite(rate) or rate <= 0.0:
        raise ValueError("采样率无效")
    x = np.asarray(input_values, dtype=float)
    y = np.asarray(output_values, dtype=float)
    count = min(x.size, y.size)
    if count < 128:
        raise ValueError("输入/输出共同样本少于128点")
    x = x[-count:]
    y = y[-count:]
    valid = np.isfinite(x) & np.isfinite(y)
    x, y = x[valid], y[valid]
    count = x.size
    segment = min(int(segment_length), count)
    segment = 2 ** int(math.floor(math.log2(segment)))
    if segment < 128:
        raise ValueError("有效分段长度小于128点")
    if float(np.std(x)) <= max(1e-9, abs(float(np.mean(x))) * 1e-6):
        raise ValueError(
            "输入给定几乎没有变化，无法估计频响；需要阶跃、扫频或PRBS激励")

    step = max(1, segment // 2)
    starts = list(range(0, count - segment + 1, step))
    if not starts:
        starts = [count - segment]
    window = np.hanning(segment)
    sxx = np.zeros(segment // 2 + 1, dtype=float)
    syy = np.zeros(segment // 2 + 1, dtype=float)
    syx = np.zeros(segment // 2 + 1, dtype=complex)
    used = 0
    for start in starts:
        xs = x[start:start + segment] - np.mean(x[start:start + segment])
        ys = y[start:start + segment] - np.mean(y[start:start + segment])
        if float(np.std(xs)) <= 1e-12:
            continue
        xf = np.fft.rfft(xs * window)
        yf = np.fft.rfft(ys * window)
        sxx += np.real(xf * np.conj(xf))
        syy += np.real(yf * np.conj(yf))
        syx += yf * np.conj(xf)
        used += 1
    if used == 0 or float(np.max(sxx)) <= 1e-20:
        raise ValueError("输入激励能量不足，无法估计频响")
    sxx /= used
    syy /= used
    syx /= used
    threshold = float(np.max(sxx)) * 1e-8
    transfer = np.full(syx.shape, np.nan + 1j * np.nan, dtype=complex)
    valid_bins = sxx > threshold
    transfer[valid_bins] = syx[valid_bins] / sxx[valid_bins]
    coherence = np.zeros_like(sxx)
    denom = sxx * syy
    coherent_bins = denom > 1e-30
    coherence[coherent_bins] = (
        np.abs(syx[coherent_bins]) ** 2 / denom[coherent_bins])
    coherence = np.clip(coherence, 0.0, 1.0)
    frequency = np.fft.rfftfreq(segment, 1.0 / rate)
    magnitude_db = 20.0 * np.log10(np.maximum(np.abs(transfer), 1e-15))
    phase_deg = np.full(sxx.shape, np.nan, dtype=float)
    phase_deg[valid_bins] = (
        np.unwrap(np.angle(transfer[valid_bins])) * 180.0 / np.pi)
    return {
        "frequency_hz": frequency,
        "magnitude_db": magnitude_db,
        "phase_deg": phase_deg,
        "coherence": coherence,
        "segment_length": segment,
        "segments": used,
        "resolution_hz": rate / segment,
    }


class FrequencyResponsePage(QWidget):
    """理论环路模型与基于已采集数据的闭环频响估计。"""

    def __init__(self, snapshot_provider: Callable[[str], dict] | None = None,
                 config_provider: Callable[[], dict] | None = None):
        super().__init__()
        self._snapshot_provider = snapshot_provider
        self._config_provider = config_provider
        root = QVBoxLayout(self)
        title = QLabel("控制环频域分析 · 波特图与传递函数")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        warning = QLabel(
            "FFT尖峰只能说明该频率含有能量，不能单独证明极限环。"
            "理论波特图用于检查带宽/裕度；实测频响必须有足够输入激励，并结合相干度判断。")
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#ffcc80;")
        root.addWidget(warning)

        tabs = QTabWidget()
        tabs.addTab(self._build_theory_tab(), "固件/理论电流环")
        tabs.addTab(self._build_measured_tab(), "实测闭环频响")
        root.addWidget(tabs, 1)

        if callable(self._config_provider):
            self._apply_firmware_config(self._config_provider())
        if _PLOT_OK:
            self._calculate_theory()

    @staticmethod
    def _spin(minimum, maximum, value, decimals=4, step=0.1, suffix=""):
        widget = QDoubleSpinBox()
        widget.setRange(minimum, maximum)
        widget.setDecimals(decimals)
        widget.setSingleStep(step)
        widget.setValue(value)
        widget.setSuffix(suffix)
        return widget

    def _build_theory_tab(self) -> QWidget:
        tab = QWidget()
        outer_layout = QVBoxLayout(tab)
        settings_tab = QWidget()
        layout = QVBoxLayout(settings_tab)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("分析模型"))
        self._model_mode = QComboBox()
        self._model_mode.addItem("固件等效离散模型（推荐）", "firmware")
        self._model_mode.addItem("连续域SI近似模型", "continuous")
        mode_row.addWidget(self._model_mode)
        self._load_firmware_btn = QPushButton("同步当前固件/UI参数")
        self._load_firmware_btn.clicked.connect(self._load_firmware_config)
        self._load_firmware_btn.setEnabled(callable(self._config_provider))
        mode_row.addWidget(self._load_firmware_btn)
        self._config_source = QLabel("参数源：工程默认值")
        self._config_source.setStyleSheet("color:#90a4ae;")
        mode_row.addWidget(self._config_source)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        controls = QGroupBox("q轴对象与控制参数")
        grid = QGridLayout(controls)
        self._r = self._spin(0.001, 100.0, 0.59, 4, 0.01, " Ω")
        self._l_mh = self._spin(0.001, 1000.0, 0.66, 4, 0.01, " mH")
        self._design_bw = self._spin(1.0, 7900.0, 700.0, 1, 10.0, " Hz")
        self._delay_us = self._spin(0.0, 1000.0, 93.75, 2, 1.0, " µs")
        self._auto_pi = QCheckBox("按带宽自动整定 Kp=Lωc，Ki=Rωc")
        self._auto_pi.setChecked(True)
        self._kp = self._spin(0.0, 100000.0, 2.902, 6, 0.01, " V/A")
        self._ki = self._spin(0.0, 1e8, 2595.0, 3, 10.0, " V/(A·s)")
        self._feedback_filter = QCheckBox("计入反馈一阶低通")
        self._feedback_filter.setChecked(False)
        self._filter_fc = self._spin(1.0, 8000.0, 2500.0, 1, 10.0, " Hz")
        fields = [
            ("相电阻 Rs", self._r), ("q轴电感 Lq", self._l_mh),
            ("设计带宽", self._design_bw), ("计算+PWM等效延迟", self._delay_us),
            ("物理比例增益 Kp", self._kp), ("物理积分增益 Ki", self._ki),
        ]
        for index, (text, widget) in enumerate(fields):
            grid.addWidget(QLabel(text), index // 2, (index % 2) * 2)
            grid.addWidget(widget, index // 2, (index % 2) * 2 + 1)
        grid.addWidget(self._auto_pi, 3, 0, 1, 2)
        grid.addWidget(self._feedback_filter, 3, 2)
        grid.addWidget(self._filter_fc, 3, 3)
        calculate = QPushButton("计算传递函数与波特图")
        calculate.setObjectName("PrimaryButton")
        calculate.clicked.connect(self._calculate_theory)
        grid.addWidget(calculate, 4, 0, 1, 4)
        firmware = QGroupBox("固件定点与digit换算（对应当前F407工程）")
        firmware_grid = QGridLayout(firmware)
        self._sample_rate = self._spin(
            1000.0, 100000.0, 16000.0, 0, 1000.0, " Hz")
        self._vbus = self._spin(1.0, 1000.0, 24.0, 2, 1.0, " V")
        self._adc_vref = self._spin(0.1, 10.0, 3.3, 3, 0.1, " V")
        self._shunt = self._spin(0.00001, 1.0, 0.01, 5, 0.001, " Ω")
        self._amp_gain = self._spin(0.01, 1000.0, 8.0, 2, 0.5, " ×")
        self._extra_delay_cycles = self._spin(
            0.0, 5.0, 0.5, 2, 0.1, " 周期")
        self._kp_digit = QSpinBox(); self._kp_digit.setRange(0, 32767)
        self._kp_digit.setValue(2323)
        self._ki_digit = QSpinBox(); self._ki_digit.setRange(0, 32767)
        self._ki_digit.setValue(2077)
        self._kp_divisor = QSpinBox(); self._kp_divisor.setRange(1, 65535)
        self._kp_divisor.setValue(1024)
        self._ki_divisor = QSpinBox(); self._ki_divisor.setRange(1, 65535)
        self._ki_divisor.setValue(16384)
        self._filter_alpha_q15 = QSpinBox()
        self._filter_alpha_q15.setRange(1, 32767)
        self._filter_alpha_q15.setValue(20491)
        firmware_fields = [
            ("FOC采样率", self._sample_rate), ("实时母线电压", self._vbus),
            ("ADC参考电压", self._adc_vref), ("采样电阻", self._shunt),
            ("运放增益", self._amp_gain),
            ("PWM额外延迟", self._extra_delay_cycles),
            ("Kp digit", self._kp_digit), ("Ki digit", self._ki_digit),
            ("Kp除数", self._kp_divisor), ("Ki除数", self._ki_divisor),
            ("反馈IIR α(Q15)", self._filter_alpha_q15),
        ]
        for index, (text, widget) in enumerate(firmware_fields):
            firmware_grid.addWidget(QLabel(text), index // 2, (index % 2) * 2)
            firmware_grid.addWidget(widget, index // 2, (index % 2) * 2 + 1)
        parameter_row = QHBoxLayout()
        parameter_row.addWidget(controls, 1)
        parameter_row.addWidget(firmware, 1)
        layout.addLayout(parameter_row)
        self._firmware_group = firmware
        self._model_mode.currentIndexChanged.connect(self._sync_model_controls)
        self._sync_model_controls()

        formula = QGroupBox("模型公式")
        formula_layout = QGridLayout(formula)

        def formula_card(title_text: str) -> tuple[QGroupBox, QLabel]:
            card = QGroupBox(title_text)
            card.setStyleSheet(
                "QGroupBox {"
                " color:#42c7ff; font-size:14px; font-weight:700;"
                " border:1px solid #344253; border-radius:8px;"
                " margin-top:10px; padding-top:8px;"
                " background-color:rgba(16,23,34,150);"
                "}"
                "QGroupBox::title {"
                " subcontrol-origin:margin; left:12px; padding:0 7px;"
                " background-color:#1b2636; border-radius:6px;"
                "}")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 14, 16, 12)
            label = QLabel()
            label.setTextFormat(Qt.RichText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            formula_font = QFont("Cambria Math", 15)
            formula_font.setWeight(QFont.DemiBold)
            label.setFont(formula_font)
            label.setMinimumHeight(78)
            label.setStyleSheet(
                "color:#f1f7fb; line-height:1.55; letter-spacing:0.2px;")
            card_layout.addWidget(label)
            return card, label

        scale_card, self._formula_scale = formula_card("① 量纲换算")
        controller_card, self._formula_controller = formula_card("② 离散控制器")
        plant_card, self._formula_plant = formula_card("③ 电机对象")
        loop_card, self._formula_loop = formula_card("④ 滤波与闭环")
        formula_layout.addWidget(scale_card, 0, 0)
        formula_layout.addWidget(controller_card, 0, 1)
        formula_layout.addWidget(plant_card, 1, 0)
        formula_layout.addWidget(loop_card, 1, 1)

        results = QGroupBox("计算结果")
        results_layout = QVBoxLayout(results)
        self._theory_metrics = QLabel("等待计算")
        self._theory_metrics.setStyleSheet("color:#80cbc4;")
        self._theory_metrics.setWordWrap(True)
        self._theory_metrics.setTextInteractionFlags(Qt.TextSelectableByMouse)
        results_layout.addWidget(self._theory_metrics)
        caveat = QLabel(
            "固件模型按当前代码换算：电流digit/A=65536·Rshunt·Gain/Vref；"
            "电压V/digit=Vbus/(√3·32768)，并复现PI除数、逐拍积分、ZOH对象和反馈IIR。"
            "2323/2077不能直接当作物理增益。"
            "该模型是解耦后的线性小信号模型，不能单独证明非线性极限环。<br>"
            "对称边带应满足 f<sub>c</sub>=(f<sub>+</sub>+f<sub>−</sub>)/2、"
            "f<sub>m</sub>=(f<sub>+</sub>−f<sub>−</sub>)/2；"
            "638/688 Hz对应中心663 Hz、调制频率25 Hz，并非50 Hz。")
        caveat.setTextFormat(Qt.RichText)
        caveat.setWordWrap(True)
        caveat.setStyleSheet("color:#90a4ae; font-size:11px;")
        results_layout.addWidget(caveat)
        layout.addWidget(formula)
        layout.addWidget(results)

        if _PLOT_OK:
            plot_tabs = QTabWidget()
            self._theory_plot_tabs = plot_tabs
            plot_tabs.addTab(settings_tab, "参数与公式")
            bode_tab = QWidget()
            plots = QHBoxLayout(bode_tab)
            self._theory_mag = pg.PlotWidget(title="幅频特性")
            self._theory_phase = pg.PlotWidget(title="相频特性")
            for plot, ylabel in ((self._theory_mag, "幅值 / dB"),
                                 (self._theory_phase, "相位 / °")):
                plot.setBackground("#10131a")
                plot.showGrid(x=True, y=True, alpha=0.3)
                plot.setLogMode(x=True, y=False)
                plot.setLabel("bottom", "频率", units="Hz")
                plot.setLabel("left", ylabel)
                plot.addLegend()
            plots.addWidget(self._theory_mag, 1)
            plots.addWidget(self._theory_phase, 1)
            plot_tabs.addTab(bode_tab, "波特图")

            nyquist_tab = QWidget()
            nyquist_layout = QVBoxLayout(nyquist_tab)
            nyquist_header = QHBoxLayout()
            self._nyquist_metrics = QLabel("等待计算奈奎斯特轨迹")
            self._nyquist_metrics.setStyleSheet("color:#80cbc4;")
            self._nyquist_metrics.setWordWrap(True)
            nyquist_header.addWidget(self._nyquist_metrics, 1)
            self._nyquist_focus = QCheckBox("聚焦临界点（±3）")
            self._nyquist_focus.setChecked(True)
            self._nyquist_focus.toggled.connect(self._refresh_nyquist_view)
            nyquist_header.addWidget(self._nyquist_focus)
            nyquist_layout.addLayout(nyquist_header)
            self._nyquist = pg.PlotWidget(title="开环奈奎斯特图 L(jω)")
            self._nyquist.setBackground("#10131a")
            self._nyquist.showGrid(x=True, y=True, alpha=0.3)
            self._nyquist.setLabel("bottom", "实部 Re{L}")
            self._nyquist.setLabel("left", "虚部 Im{L}")
            self._nyquist.setAspectLocked(True, ratio=1.0)
            self._nyquist.addLegend()
            nyquist_layout.addWidget(self._nyquist, 1)
            plot_tabs.addTab(nyquist_tab, "奈奎斯特图")

            pole_tab = QWidget()
            pole_layout = QVBoxLayout(pole_tab)
            self._pole_metrics = QLabel("等待计算闭环零极点")
            self._pole_metrics.setStyleSheet("color:#80cbc4;")
            self._pole_metrics.setWordWrap(True)
            pole_layout.addWidget(self._pole_metrics)
            pole_content = QHBoxLayout()
            self._pole_plot = pg.PlotWidget(title="闭环零极点图")
            self._pole_plot.setBackground("#10131a")
            self._pole_plot.showGrid(x=True, y=True, alpha=0.3)
            self._pole_plot.setAspectLocked(True, ratio=1.0)
            self._pole_plot.addLegend()
            pole_content.addWidget(self._pole_plot, 3)
            self._pole_table = QTableWidget(0, 7)
            self._pole_table.setHorizontalHeaderLabels(
                ["类型", "实部", "虚部", "模长", "阻尼比", "等效频率/Hz", "判定"])
            self._pole_table.setEditTriggers(QTableWidget.NoEditTriggers)
            self._pole_table.setSelectionBehavior(QTableWidget.SelectRows)
            self._pole_table.verticalHeader().setVisible(False)
            self._pole_table.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeToContents)
            self._pole_table.horizontalHeader().setStretchLastSection(True)
            pole_content.addWidget(self._pole_table, 2)
            pole_layout.addLayout(pole_content, 1)
            plot_tabs.addTab(pole_tab, "零极点图")

            sensitivity_tab = QWidget()
            sensitivity_layout = QVBoxLayout(sensitivity_tab)
            self._sensitivity_metrics = QLabel("等待计算灵敏度函数")
            self._sensitivity_metrics.setStyleSheet("color:#80cbc4;")
            self._sensitivity_metrics.setWordWrap(True)
            sensitivity_layout.addWidget(self._sensitivity_metrics)
            sensitivity_note = QLabel(
                "S=1/(1+L) 表示误差/扰动灵敏度；T=L/(1+L) 表示反馈点互补灵敏度；"
                "U/R=C/(1+L) 表示每1 A参考电流所需的控制电压。")
            sensitivity_note.setStyleSheet("color:#90a4ae;")
            sensitivity_note.setWordWrap(True)
            sensitivity_layout.addWidget(sensitivity_note)
            self._sensitivity_plot = pg.PlotWidget(title="灵敏度与控制作用频响")
            self._sensitivity_plot.setBackground("#10131a")
            self._sensitivity_plot.showGrid(x=True, y=True, alpha=0.3)
            self._sensitivity_plot.setLogMode(x=True, y=False)
            self._sensitivity_plot.setLabel("bottom", "频率", units="Hz")
            self._sensitivity_plot.setLabel("left", "幅值", units="dB")
            self._sensitivity_plot.addLegend()
            sensitivity_layout.addWidget(self._sensitivity_plot, 1)
            plot_tabs.addTab(sensitivity_tab, "灵敏度 S/T")

            locus_tab = QWidget()
            locus_layout = QVBoxLayout(locus_tab)
            locus_controls = QHBoxLayout()
            locus_controls.addWidget(QLabel("扫描参数"))
            self._locus_parameter = QComboBox()
            self._locus_parameter.addItem("Kp 倍率", "kp_scale")
            self._locus_parameter.addItem("Ki 倍率", "ki_scale")
            self._locus_parameter.addItem("Kp/Ki 同步倍率", "pi_scale")
            self._locus_parameter.addItem("反馈 IIR α", "filter_alpha")
            locus_controls.addWidget(self._locus_parameter)
            locus_controls.addWidget(QLabel("起点"))
            self._locus_start = self._spin(0.0, 10.0, 0.2, 3, 0.1)
            locus_controls.addWidget(self._locus_start)
            locus_controls.addWidget(QLabel("终点"))
            self._locus_stop = self._spin(0.0, 10.0, 2.0, 3, 0.1)
            locus_controls.addWidget(self._locus_stop)
            locus_controls.addWidget(QLabel("点数"))
            self._locus_points = QSpinBox()
            self._locus_points.setRange(8, 101)
            self._locus_points.setValue(31)
            locus_controls.addWidget(self._locus_points)
            locus_calculate = QPushButton("扫描根轨迹")
            locus_calculate.setObjectName("PrimaryButton")
            locus_calculate.clicked.connect(self._calculate_root_locus)
            locus_controls.addWidget(locus_calculate)
            locus_controls.addStretch(1)
            locus_layout.addLayout(locus_controls)
            self._locus_metrics = QLabel("等待扫描")
            self._locus_metrics.setStyleSheet("color:#80cbc4;")
            self._locus_metrics.setWordWrap(True)
            locus_layout.addWidget(self._locus_metrics)
            locus_content = QHBoxLayout()
            self._locus_plot = pg.PlotWidget(title="闭环根轨迹")
            self._locus_plot.setBackground("#10131a")
            self._locus_plot.showGrid(x=True, y=True, alpha=0.3)
            self._locus_plot.setAspectLocked(True, ratio=1.0)
            self._locus_plot.addLegend()
            locus_content.addWidget(self._locus_plot, 3)
            self._locus_table = QTableWidget(0, 3)
            self._locus_table.setHorizontalHeaderLabels(
                ["扫描值", "最大极点模长", "判定"])
            self._locus_table.setEditTriggers(QTableWidget.NoEditTriggers)
            self._locus_table.verticalHeader().setVisible(False)
            self._locus_table.horizontalHeader().setSectionResizeMode(
                QHeaderView.Stretch)
            locus_content.addWidget(self._locus_table, 1)
            locus_layout.addLayout(locus_content, 1)
            self._locus_parameter.currentIndexChanged.connect(
                self._sync_locus_parameter)
            self._sync_locus_parameter()
            plot_tabs.addTab(locus_tab, "根轨迹/参数扫描")
            outer_layout.addWidget(plot_tabs, 1)
        else:
            layout.addWidget(QLabel("缺少numpy/pyqtgraph，无法绘制波特图"), 1)
            outer_layout.addWidget(settings_tab, 1)
        return tab

    def _refresh_nyquist_view(self, *_args) -> None:
        if not _PLOT_OK or not hasattr(self, "_nyquist"):
            return
        if self._nyquist_focus.isChecked():
            self._nyquist.disableAutoRange()
            self._nyquist.setXRange(-3.0, 3.0, padding=0.02)
            self._nyquist.setYRange(-3.0, 3.0, padding=0.02)
        else:
            self._nyquist.enableAutoRange()

    def _update_pole_plot(self, result: dict, sample_rate_hz: float) -> None:
        poles = np.asarray(result["poles"], dtype=complex)
        zeros = np.asarray(result["zeros"], dtype=complex)
        discrete = result["pole_domain"] == "z"
        self._pole_plot.clear()
        self._pole_plot.addLegend()
        if discrete:
            angle = np.linspace(0.0, 2.0 * np.pi, 361)
            self._pole_plot.plot(
                np.cos(angle), np.sin(angle),
                pen=pg.mkPen("#78909c", width=1.3, style=Qt.DashLine),
                name="单位圆（稳定边界）")
            self._pole_plot.setLabel("bottom", "实部 Re{z}")
            self._pole_plot.setLabel("left", "虚部 Im{z}")
            self._pole_plot.disableAutoRange()
            self._pole_plot.setXRange(-1.35, 1.35, padding=0.02)
            self._pole_plot.setYRange(-1.35, 1.35, padding=0.02)
        else:
            self._pole_plot.addItem(pg.InfiniteLine(
                pos=0.0, angle=90,
                pen=pg.mkPen("#ef5350", width=1.3, style=Qt.DashLine)))
            self._pole_plot.setLabel("bottom", "实部 Re{s}", units="rad/s")
            self._pole_plot.setLabel("left", "虚部 Im{s}", units="rad/s")
            self._pole_plot.enableAutoRange()
        if zeros.size:
            self._pole_plot.addItem(pg.ScatterPlotItem(
                np.real(zeros), np.imag(zeros), symbol="o", size=13,
                pen=pg.mkPen("#42c7ff", width=2), brush=None,
                name="闭环零点"))
        if poles.size:
            self._pole_plot.addItem(pg.ScatterPlotItem(
                np.real(poles), np.imag(poles), symbol="x", size=15,
                pen=pg.mkPen("#ff5252", width=3), brush=None,
                name="闭环极点"))

        if discrete:
            ordered_poles = sorted(poles, key=abs, reverse=True)
        else:
            ordered_poles = sorted(poles, key=lambda value: value.real,
                                   reverse=True)
        entries = [("极点", value) for value in ordered_poles]
        entries += [("零点", value) for value in zeros]
        self._pole_table.setRowCount(len(entries))
        stable_count = 0
        for row, (kind, value) in enumerate(entries):
            magnitude = abs(value)
            if discrete and magnitude > 1e-15:
                mapped_s = sample_rate_hz * complex(
                    math.log(magnitude), math.atan2(value.imag, value.real))
                natural = abs(mapped_s)
                damping = (-mapped_s.real / natural
                           if natural > 1e-12 else 1.0)
                frequency_hz = abs(mapped_s.imag) / (2.0 * math.pi)
                stable = magnitude < 1.0
            elif discrete:
                damping, frequency_hz, stable = 1.0, 0.0, True
            else:
                natural = magnitude
                damping = (-value.real / natural
                           if natural > 1e-12 else 1.0)
                frequency_hz = abs(value.imag) / (2.0 * math.pi)
                stable = value.real < 0.0
            if kind == "极点" and stable:
                stable_count += 1
            verdict = ("稳定" if stable else "不稳定") if kind == "极点" else "—"
            values = [
                kind, f"{value.real:.6g}", f"{value.imag:+.6g}",
                f"{magnitude:.6g}",
                f"{damping:.4f}" if kind == "极点" else "—",
                f"{frequency_hz:.3f}" if kind == "极点" else "—",
                verdict,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if kind == "极点" and not stable:
                    item.setForeground(Qt.red)
                self._pole_table.setItem(row, column, item)

        if poles.size:
            dominant = ordered_poles[0]
            if discrete:
                criterion = f"最大极点模长 |p|max={abs(dominant):.6f}"
                boundary = "全部位于单位圆内" if stable_count == poles.size else "存在单位圆外极点"
            else:
                criterion = f"最右极点 Re(p)={dominant.real:.6g} rad/s"
                boundary = "全部位于左半平面" if stable_count == poles.size else "存在右半平面极点"
            self._pole_metrics.setText(
                f"{boundary}；{criterion}。　{result['pole_model_note']}。"
                "表中离散极点的频率与阻尼由 s=ln(z)/Ts 映射得到。")
        else:
            self._pole_metrics.setText("当前模型没有可显示的有限闭环极点。")

    def _update_sensitivity_plot(self, result: dict) -> None:
        frequency = result["frequency_hz"]
        self._sensitivity_plot.clear()
        self._sensitivity_plot.addLegend()
        self._sensitivity_plot.plot(
            frequency, result["sensitivity_db"],
            pen=pg.mkPen("#ffb74d", width=1.8), name="S：扰动/误差灵敏度")
        self._sensitivity_plot.plot(
            frequency, result["complementary_db"],
            pen=pg.mkPen("#42c7ff", width=1.8), name="T：互补灵敏度")
        self._sensitivity_plot.plot(
            frequency, result["control_response_db"],
            pen=pg.mkPen("#ce93d8", width=1.5), name="U/R：控制电压需求")
        self._sensitivity_plot.addItem(pg.InfiniteLine(
            pos=0.0, angle=0,
            pen=pg.mkPen("#546e7a", style=Qt.DashLine)))
        ms = result["ms"]
        if ms <= 1.5:
            assessment = "鲁棒性较好"
        elif ms <= 2.0:
            assessment = "鲁棒性一般，建议结合参数漂移检查"
        else:
            assessment = "灵敏度峰值偏高，存在明显谐振/脆弱性"
        control_peak_index = int(np.argmax(result["control_response_db"]))
        self._sensitivity_metrics.setText(
            f"Ms={ms:.3f}（{20.0 * math.log10(ms):.2f} dB @ "
            f"{result['ms_frequency_hz']:.1f} Hz）；"
            f"Mt={result['mt']:.3f}（{20.0 * math.log10(result['mt']):.2f} dB @ "
            f"{result['mt_frequency_hz']:.1f} Hz）。{assessment}。　"
            f"U/R峰值={result['control_response_db'][control_peak_index]:.2f} dB(V/A) @ "
            f"{frequency[control_peak_index]:.1f} Hz。")

    def _sync_locus_parameter(self, *_args) -> None:
        if not hasattr(self, "_locus_parameter"):
            return
        is_alpha = self._locus_parameter.currentData() == "filter_alpha"
        for widget in (self._locus_start, self._locus_stop):
            widget.blockSignals(True)
            widget.setSuffix("" if is_alpha else " ×")
            widget.setDecimals(4 if is_alpha else 3)
            widget.setSingleStep(0.05 if is_alpha else 0.1)
            widget.setRange(0.001 if is_alpha else 0.01,
                            0.9999 if is_alpha else 5.0)
            widget.blockSignals(False)
        if is_alpha:
            self._locus_start.setValue(0.10)
            self._locus_stop.setValue(0.95)
        else:
            self._locus_start.setValue(0.20)
            self._locus_stop.setValue(2.00)

    @staticmethod
    def _track_root_branches(root_sets):
        if not root_sets or not root_sets[0]:
            return []
        count = len(root_sets[0])
        if any(len(roots) != count for roots in root_sets):
            return []
        first = sorted(root_sets[0], key=lambda value: (value.imag, value.real))
        branches = [[value] for value in first]
        previous = first
        for roots in root_sets[1:]:
            remaining = list(roots)
            ordered = []
            for prior in previous:
                index = min(range(len(remaining)),
                            key=lambda item: abs(remaining[item] - prior))
                ordered.append(remaining.pop(index))
            for branch, value in zip(branches, ordered):
                branch.append(value)
            previous = ordered
        return branches

    def _calculate_root_locus(self, *_args) -> None:
        if not _PLOT_OK or not hasattr(self, "_locus_plot"):
            return
        start = self._locus_start.value()
        stop = self._locus_stop.value()
        if not start < stop:
            self._locus_metrics.setText("扫描起点必须小于终点。")
            return
        parameter = self._locus_parameter.currentData()
        firmware_mode = self._model_mode.currentData() == "firmware"
        if parameter == "filter_alpha" and not firmware_mode:
            self._locus_metrics.setText("反馈IIR α扫描只适用于固件离散模型。")
            return
        if parameter == "filter_alpha" and not self._feedback_filter.isChecked():
            self._locus_metrics.setText("反馈IIR当前处于旁路；请先勾选“计入反馈一阶低通”。")
            return
        values = np.linspace(start, stop, self._locus_points.value())
        root_sets = []
        stability_metric = []
        stable_flags = []
        try:
            for value in values:
                if firmware_mode:
                    kp_digit = self._kp_digit.value()
                    ki_digit = self._ki_digit.value()
                    alpha_q15 = self._filter_alpha_q15.value()
                    if parameter in ("kp_scale", "pi_scale"):
                        kp_digit = max(0, min(32767, round(kp_digit * value)))
                    if parameter in ("ki_scale", "pi_scale"):
                        ki_digit = max(0, min(32767, round(ki_digit * value)))
                    if parameter == "filter_alpha":
                        alpha_q15 = max(1, min(32767, round(value * 32768.0)))
                    scan_result = firmware_current_loop_frequency_response(
                        self._r.value(), self._l_mh.value() / 1000.0,
                        self._sample_rate.value(), self._vbus.value(),
                        self._adc_vref.value(), self._shunt.value(),
                        self._amp_gain.value(), kp_digit, ki_digit,
                        self._kp_divisor.value(), self._ki_divisor.value(),
                        self._extra_delay_cycles.value(),
                        self._feedback_filter.isChecked(), alpha_q15,
                        points=64)
                    roots = list(scan_result["poles"])
                    metric = max((abs(root) for root in roots), default=0.0)
                    stable = metric < 1.0
                else:
                    kp = self._kp.value()
                    ki = self._ki.value()
                    if parameter in ("kp_scale", "pi_scale"):
                        kp *= value
                    if parameter in ("ki_scale", "pi_scale"):
                        ki *= value
                    scan_result = current_loop_frequency_response(
                        self._r.value(), self._l_mh.value() / 1000.0,
                        kp, ki, self._delay_us.value() * 1e-6,
                        self._filter_fc.value()
                        if self._feedback_filter.isChecked() else None,
                        points=64)
                    roots = list(scan_result["poles"])
                    metric = max((root.real for root in roots), default=-math.inf)
                    stable = metric < 0.0
                root_sets.append(roots)
                stability_metric.append(metric)
                stable_flags.append(stable)
        except ValueError as exc:
            self._locus_metrics.setText(f"根轨迹无法计算：{exc}")
            return

        self._locus_plot.clear()
        self._locus_plot.addLegend()
        if firmware_mode:
            circle = np.linspace(0.0, 2.0 * np.pi, 361)
            self._locus_plot.plot(
                np.cos(circle), np.sin(circle),
                pen=pg.mkPen("#78909c", width=1.2, style=Qt.DashLine),
                name="单位圆（稳定边界）")
            self._locus_plot.setLabel("bottom", "实部 Re{z}")
            self._locus_plot.setLabel("left", "虚部 Im{z}")
            self._locus_plot.disableAutoRange()
            self._locus_plot.setXRange(-1.35, 1.35, padding=0.02)
            self._locus_plot.setYRange(-1.35, 1.35, padding=0.02)
            current_value = (self._filter_alpha_q15.value() / 32768.0
                             if parameter == "filter_alpha" else 1.0)
            metric_title = "最大极点模长"
        else:
            self._locus_plot.addItem(pg.InfiniteLine(
                pos=0.0, angle=90,
                pen=pg.mkPen("#ef5350", width=1.2, style=Qt.DashLine)))
            self._locus_plot.setLabel("bottom", "实部 Re{s}", units="rad/s")
            self._locus_plot.setLabel("left", "虚部 Im{s}", units="rad/s")
            self._locus_plot.enableAutoRange()
            current_value = 1.0
            metric_title = "最右极点实部"

        branches = self._track_root_branches(root_sets)
        for index, branch in enumerate(branches):
            color = pg.intColor(index, hues=max(3, len(branches)), values=1,
                                maxValue=230)
            self._locus_plot.plot(
                [value.real for value in branch],
                [value.imag for value in branch],
                pen=pg.mkPen(color, width=1.8), name=f"极点支路 {index + 1}")
        all_roots = [root for roots in root_sets for root in roots]
        if not branches and all_roots:
            self._locus_plot.addItem(pg.ScatterPlotItem(
                [root.real for root in all_roots], [root.imag for root in all_roots],
                symbol="o", size=4, pen=None, brush="#42c7ff"))
        if root_sets:
            self._locus_plot.addItem(pg.ScatterPlotItem(
                [root.real for root in root_sets[0]],
                [root.imag for root in root_sets[0]],
                symbol="o", size=10, pen=pg.mkPen("#69f0ae", width=2),
                brush=None, name="扫描起点"))
            self._locus_plot.addItem(pg.ScatterPlotItem(
                [root.real for root in root_sets[-1]],
                [root.imag for root in root_sets[-1]],
                symbol="t", size=11, pen=pg.mkPen("#ffb74d", width=2),
                brush=None, name="扫描终点"))
            current_index = int(np.argmin(np.abs(values - current_value)))
            self._locus_plot.addItem(pg.ScatterPlotItem(
                [root.real for root in root_sets[current_index]],
                [root.imag for root in root_sets[current_index]],
                symbol="x", size=14, pen=pg.mkPen("#ff5252", width=3),
                brush=None, name="当前参数附近"))

        self._locus_table.setHorizontalHeaderLabels(
            ["扫描值", metric_title, "判定"])
        self._locus_table.setRowCount(len(values))
        for row, (value, metric, stable) in enumerate(
                zip(values, stability_metric, stable_flags)):
            row_values = [f"{value:.5g}", f"{metric:.6g}",
                          "稳定" if stable else "不稳定"]
            for column, text in enumerate(row_values):
                item = QTableWidgetItem(text)
                if not stable:
                    item.setForeground(Qt.red)
                self._locus_table.setItem(row, column, item)
        stable_values = values[np.asarray(stable_flags, dtype=bool)]
        if stable_values.size:
            stable_text = f"采样点稳定范围：{stable_values[0]:.5g}～{stable_values[-1]:.5g}"
        else:
            stable_text = "扫描范围内没有稳定采样点"
        transitions = [
            (values[index - 1], values[index])
            for index in range(1, len(values))
            if stable_flags[index] != stable_flags[index - 1]
        ]
        transition_text = ("；边界位于 " + "、".join(
            f"{left:.5g}～{right:.5g}" for left, right in transitions)
            if transitions else "；扫描范围内未发现稳定性穿越")
        self._locus_metrics.setText(
            f"{self._locus_parameter.currentText()}：{stable_text}{transition_text}。"
            "边界仅精确到当前扫描步长，可增加点数细化。")

    def _build_measured_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        controls = QGroupBox("Welch H1闭环频响估计")
        row = QHBoxLayout(controls)
        row.addWidget(QLabel("输入→输出"))
        self._pair = QComboBox()
        self._pair.addItem("Iq给定 → Iq实际", ("iqref", "iq", "A/A"))
        self._pair.addItem("转速给定 → 实际转速", ("speedref", "speed", "rpm/rpm"))
        row.addWidget(self._pair)
        row.addWidget(QLabel("Welch分段"))
        self._segment = QSpinBox()
        self._segment.setRange(128, 8192)
        self._segment.setSingleStep(128)
        self._segment.setValue(1024)
        self._segment.setSuffix(" 点")
        row.addWidget(self._segment)
        analyze = QPushButton("估计实测闭环频响")
        analyze.setObjectName("PrimaryButton")
        analyze.clicked.connect(self._calculate_measured)
        row.addWidget(analyze)
        row.addStretch(1)
        layout.addWidget(controls)

        explanation = QLabel(
            "H₁(f)=Sᵧₓ(f)/Sₓₓ(f)；　"
            "γ²(f)=|Sₓᵧ(f)|²/[Sₓₓ(f)Sᵧᵧ(f)]。"
            "γ²接近1时该频点可信；给定恒定或只有一次阶跃时，不能把结果当作完整波特图。")
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color:#90a4ae;")
        layout.addWidget(explanation)
        self._measured_metrics = QLabel("等待分析已采集的同步输入/输出缓冲")
        self._measured_metrics.setWordWrap(True)
        self._measured_metrics.setStyleSheet("color:#80cbc4;")
        layout.addWidget(self._measured_metrics)

        if _PLOT_OK:
            plot_row = QHBoxLayout()
            self._measured_mag = pg.PlotWidget(title="实测闭环幅频")
            self._measured_phase = pg.PlotWidget(title="相位与相干度")
            for plot in (self._measured_mag, self._measured_phase):
                plot.setBackground("#10131a")
                plot.showGrid(x=True, y=True, alpha=0.3)
                plot.setLogMode(x=True, y=False)
                plot.setLabel("bottom", "频率", units="Hz")
                plot.addLegend()
            self._measured_mag.setLabel("left", "幅值", units="dB")
            self._measured_phase.setLabel("left", "相位 / °；相干度×100")
            plot_row.addWidget(self._measured_mag, 1)
            plot_row.addWidget(self._measured_phase, 1)
            layout.addLayout(plot_row, 1)
        else:
            layout.addWidget(QLabel("缺少numpy/pyqtgraph，无法估计频响"), 1)
        return tab

    def _sync_model_controls(self, *_args) -> None:
        firmware_mode = self._model_mode.currentData() == "firmware"
        self._firmware_group.setEnabled(firmware_mode)
        self._design_bw.setEnabled(not firmware_mode)
        self._delay_us.setEnabled(not firmware_mode)
        self._auto_pi.setEnabled(not firmware_mode)
        self._kp.setEnabled(not firmware_mode)
        self._ki.setEnabled(not firmware_mode)
        self._filter_fc.setEnabled(not firmware_mode)

    def _apply_firmware_config(self, config: dict) -> None:
        self._kp_digit.setValue(int(config.get("kp_cur_digit", 2323)))
        self._ki_digit.setValue(int(config.get("ki_cur_digit", 2077)))
        self._sample_rate.setValue(float(config.get("sample_rate_hz", 16000)))
        self._vbus.setValue(float(config.get("vbus_v", 24.0)))
        alpha_q15 = int(config.get("filter_alpha_q15", 20491))
        alpha_q15 = max(1, min(32767, alpha_q15))
        self._filter_alpha_q15.setValue(alpha_q15)
        self._feedback_filter.setChecked(bool(config.get("filter_enabled", False)))
        alpha = alpha_q15 / 32768.0
        cutoff = (-self._sample_rate.value() / (2.0 * math.pi) *
                  math.log(1.0 - alpha))
        self._filter_fc.setValue(cutoff)
        self._config_source.setText(
            f"参数源：{config.get('source', '当前配置')}")

    def _load_firmware_config(self, *_args) -> None:
        if not callable(self._config_provider):
            return
        self._apply_firmware_config(self._config_provider())
        self._model_mode.setCurrentIndex(
            self._model_mode.findData("firmware"))
        self._sync_model_controls()
        self._calculate_theory()

    def _calculate_theory(self) -> None:
        if not _PLOT_OK:
            return
        firmware_mode = self._model_mode.currentData() == "firmware"
        try:
            if firmware_mode:
                sample_rate = self._sample_rate.value()
                result = firmware_current_loop_frequency_response(
                    self._r.value(), self._l_mh.value() / 1000.0,
                    sample_rate, self._vbus.value(), self._adc_vref.value(),
                    self._shunt.value(), self._amp_gain.value(),
                    self._kp_digit.value(), self._ki_digit.value(),
                    self._kp_divisor.value(), self._ki_divisor.value(),
                    self._extra_delay_cycles.value(),
                    self._feedback_filter.isChecked(),
                    self._filter_alpha_q15.value())
                self._kp.setValue(result["kp_physical"])
                self._ki.setValue(result["ki_continuous_equivalent"])
                self._formula_scale.setText(
                    "I<sub>digit</sub>=K<sub>I</sub>·I<sub>A</sub><br>"
                    "K<sub>I</sub>=65536·R<sub>shunt</sub>·G/V<sub>ref</sub>"
                    "<br><br>V=K<sub>V</sub>·V<sub>digit</sub><br>"
                    "K<sub>V</sub>=V<sub>bus</sub>/(√3·32768)")
                self._formula_controller.setText(
                    "C(z)=K<sub>I</sub>K<sub>V</sub>·[ Kp<sub>d</sub>/D<sub>p</sub>"
                    "<br>＋ (Ki<sub>d</sub>/D<sub>i</sub>)/(1−z<sup>−1</sup>) ]"
                    "<br><br>积分器每个16 kHz控制周期累加一次当前误差。")
                self._formula_plant.setText(
                    "a=e<sup>−R·T<sub>s</sub>/L</sup>　，　b=(1−a)/R"
                    "<br><br>G<sub>p</sub>(z)=b·z<sup>−1</sup> / "
                    "(1−a·z<sup>−1</sup>)"
                    "<br><br>z<sup>−1</sup>表示ZOH对象的一拍状态更新。")
                self._formula_loop.setText(
                    "H<sub>f</sub>(z)=α / [1−(1−α)z<sup>−1</sup>]"
                    "<br><br>L(z)=C(z)G<sub>p</sub>(z)z<sup>−d</sup>H<sub>f</sub>(z)"
                    "<br>T<sub>raw</sub>(z)=CG<sub>p</sub>z<sup>−d</sup> / [1＋L(z)]"
                    "<br>T<sub>fb</sub>(z)=H<sub>f</sub>(z)T<sub>raw</sub>(z)")
            else:
                if self._auto_pi.isChecked():
                    omega_c = 2.0 * math.pi * self._design_bw.value()
                    kp = self._l_mh.value() / 1000.0 * omega_c
                    ki = self._r.value() * omega_c
                    self._kp.setValue(kp)
                    self._ki.setValue(ki)
                result = current_loop_frequency_response(
                    self._r.value(), self._l_mh.value() / 1000.0,
                    self._kp.value(), self._ki.value(),
                    self._delay_us.value() * 1e-6,
                    self._filter_fc.value() if self._feedback_filter.isChecked()
                    else None)
                self._formula_scale.setText(
                    "连续域模型直接使用SI量纲。<br><br>"
                    "输入：电流误差 [A]<br>输出：q轴电压 [V]")
                self._formula_controller.setText(
                    "C(s)=K<sub>p</sub>＋K<sub>i</sub>/s"
                    "<br><br>K<sub>p</sub>=L·ω<sub>c</sub>"
                    "<br>K<sub>i</sub>=R·ω<sub>c</sub>")
                self._formula_plant.setText(
                    "G<sub>p</sub>(s)=1/(L<sub>q</sub>s＋R<sub>s</sub>)"
                    "<br><br>G<sub>d</sub>(s)=e<sup>−sT<sub>d</sub></sup>")
                self._formula_loop.setText(
                    "H<sub>f</sub>(s)=ω<sub>f</sub>/(s＋ω<sub>f</sub>)"
                    "<br><br>L(s)=C(s)G<sub>p</sub>(s)G<sub>d</sub>(s)H<sub>f</sub>(s)"
                    "<br>T(s)=CG<sub>p</sub>G<sub>d</sub>/[1＋L(s)]")
        except ValueError as exc:
            QMessageBox.warning(self, "模型无法计算", str(exc))
            return
        frequency = result["frequency_hz"]
        self._theory_mag.clear()
        self._theory_mag.addLegend()
        self._theory_mag.plot(
            frequency, result["plant_db"],
            pen=pg.mkPen("#90a4ae", width=1.2), name="对象 Gp")
        self._theory_mag.plot(
            frequency, result["loop_db"],
            pen=pg.mkPen("#ffb74d", width=1.6), name="开环 L")
        self._theory_mag.plot(
            frequency, result["closed_db"],
            pen=pg.mkPen("#4fc3f7", width=1.8), name="闭环实际电流")
        if firmware_mode and self._feedback_filter.isChecked():
            self._theory_mag.plot(
                frequency, result["feedback_db"],
                pen=pg.mkPen("#69f0ae", width=1.4), name="PI所见滤波电流")
        self._theory_mag.addItem(pg.InfiniteLine(
            pos=0.0, angle=0, pen=pg.mkPen("#546e7a", style=Qt.DashLine)))
        if math.isfinite(result["gain_cross_hz"]):
            self._theory_mag.addItem(pg.InfiniteLine(
                pos=result["gain_cross_hz"], angle=90,
                pen=pg.mkPen("#ffb74d", style=Qt.DashLine),
                label=f"开环0 dB {result['gain_cross_hz']:.1f} Hz"))
        if math.isfinite(result["closed_bandwidth_hz"]):
            self._theory_mag.addItem(pg.InfiniteLine(
                pos=result["closed_bandwidth_hz"], angle=90,
                pen=pg.mkPen("#4fc3f7", style=Qt.DashLine),
                label=f"闭环BW {result['closed_bandwidth_hz']:.1f} Hz"))
        self._theory_phase.clear()
        self._theory_phase.addLegend()
        self._theory_phase.plot(
            frequency, result["loop_phase_deg"],
            pen=pg.mkPen("#ffb74d", width=1.6), name="开环相位")
        self._theory_phase.plot(
            frequency, result["closed_phase_deg"],
            pen=pg.mkPen("#4fc3f7", width=1.8), name="闭环相位")
        self._theory_phase.addItem(pg.InfiniteLine(
            pos=-180.0, angle=0,
            pen=pg.mkPen("#ef5350", style=Qt.DashLine)))

        loop_complex = result["loop_complex"]
        self._nyquist.clear()
        self._nyquist.addLegend()
        circle_angle = np.linspace(0.0, 2.0 * np.pi, 361)
        self._nyquist.plot(
            np.cos(circle_angle), np.sin(circle_angle),
            pen=pg.mkPen("#546e7a", width=1.0, style=Qt.DashLine),
            name="单位圆（参考）")
        self._nyquist.plot(
            np.real(loop_complex), np.imag(loop_complex),
            pen=pg.mkPen("#42c7ff", width=2.0), name="正频率 0→Nyquist")
        self._nyquist.plot(
            np.real(loop_complex[::-1]), -np.imag(loop_complex[::-1]),
            pen=pg.mkPen("#7e57c2", width=1.5), name="负频率镜像")
        critical = pg.ScatterPlotItem(
            [-1.0], [0.0], symbol="x", size=16,
            pen=pg.mkPen("#ff5252", width=3), brush=None,
            name="临界点 (−1, 0)")
        self._nyquist.addItem(critical)
        critical_label = pg.TextItem("  临界点 (−1, 0)", color="#ff8a80")
        critical_label.setPos(-1.0, 0.0)
        self._nyquist.addItem(critical_label)
        self._nyquist.plot(
            [float(np.real(loop_complex[0]))],
            [float(np.imag(loop_complex[0]))],
            pen=None, symbol="o", symbolSize=8,
            symbolBrush="#69f0ae", symbolPen=None, name="最低分析频率")
        self._nyquist_metrics.setText(
            "判稳关键是开环轨迹与临界点 (−1,0) 的关系，不是是否进入单位圆。　"
            f"采样频率范围内距临界点最近：{result['nyquist_min_distance']:.4g}，"
            f"对应 {result['nyquist_min_distance_hz']:.1f} Hz。")
        self._refresh_nyquist_view()
        self._update_pole_plot(
            result, self._sample_rate.value() if firmware_mode else 1.0)
        self._update_sensitivity_plot(result)
        self._calculate_root_locus()
        gm = result["gain_margin_db"]
        gm_text = f"{gm:.1f} dB" if math.isfinite(gm) else "∞/范围内无−180°穿越"
        conversion_text = ""
        if firmware_mode:
            conversion_text = (
                f"<br>换算：1 A={result['current_digit_per_amp']:.3f} current digit；"
                f"1 voltage digit={result['volt_per_voltage_digit'] * 1000.0:.6f} mV；"
                f"Kp={result['kp_physical']:.6g} V/A；"
                f"Ki每拍={result['ki_step_physical']:.6g} V/A，"
                f"等效连续Ki={result['ki_continuous_equivalent']:.3f} V/(A·s)；"
                f"总等效延迟≈{result['total_delay_cycles']:.2f}周期。"
                + (f"反馈IIR α={result['filter_alpha']:.6f}，"
                   f"fc≈{result['filter_cutoff_hz']:.1f} Hz。"
                   if self._feedback_filter.isChecked() else "反馈IIR已旁路。"))
            if self._feedback_filter.isChecked():
                conversion_text += (
                    f"<br>PI所见滤波反馈−3 dB带宽："
                    f"{result['feedback_bandwidth_hz']:.1f} Hz；"
                    f"峰值{result['feedback_resonance_db']:.2f} dB @ "
                    f"{result['feedback_resonance_hz']:.1f} Hz。")
        self._theory_metrics.setText(
            f"增益交越：{result['gain_cross_hz']:.1f} Hz　｜　"
            f"相位裕度：{result['phase_margin_deg']:.1f}°　｜　"
            f"增益裕度：{gm_text}<br>"
            f"闭环−3 dB带宽：{result['closed_bandwidth_hz']:.1f} Hz　｜　"
            f"闭环峰值：{result['resonance_db']:.2f} dB @ "
            f"{result['resonance_hz']:.1f} Hz{conversion_text}")

    def _calculate_measured(self) -> None:
        if not _PLOT_OK or self._snapshot_provider is None:
            QMessageBox.warning(self, "无法分析", "当前监控缓冲不可用")
            return
        input_key, output_key, unit = self._pair.currentData()
        try:
            input_snapshot = self._snapshot_provider(input_key)
            output_snapshot = self._snapshot_provider(output_key)
            input_rate = float(input_snapshot.get("sample_rate_hz", 0.0))
            output_rate = float(output_snapshot.get("sample_rate_hz", 0.0))
            if input_rate <= 0.0 or output_rate <= 0.0:
                raise ValueError("输入或输出没有有效采样率")
            if abs(input_rate - output_rate) > max(input_rate, output_rate) * 0.01:
                raise ValueError("输入与输出采样率不一致，不能直接估计频响")
            result = estimate_welch_frf(
                input_snapshot.get("values", ()),
                output_snapshot.get("values", ()), input_rate,
                self._segment.value())
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "实测频响无法计算", str(exc))
            return
        frequency = result["frequency_hz"]
        valid = (frequency > 0.0) & np.isfinite(result["magnitude_db"])
        frequency = frequency[valid]
        magnitude = result["magnitude_db"][valid]
        phase = result["phase_deg"][valid]
        coherence = result["coherence"][valid]
        self._measured_mag.clear()
        self._measured_mag.addLegend()
        self._measured_mag.plot(
            frequency, magnitude, pen=pg.mkPen("#4fc3f7", width=1.7),
            name=f"H1 ({unit})")
        self._measured_phase.clear()
        self._measured_phase.addLegend()
        self._measured_phase.plot(
            frequency, phase, pen=pg.mkPen("#ffb74d", width=1.5),
            name="相位")
        self._measured_phase.plot(
            frequency, coherence * 100.0,
            pen=pg.mkPen("#69f0ae", width=1.3), name="相干度×100")
        trusted = coherence >= 0.8
        trusted_ratio = float(np.mean(trusted) * 100.0) if trusted.size else 0.0
        output_processing = str(
            output_snapshot.get("source_processing", "输出处理链未声明"))
        self._measured_metrics.setText(
            f"Welch：{result['segments']}段、每段{result['segment_length']}点、"
            f"分辨率{result['resolution_hz']:.3f} Hz；"
            f"相干度≥0.8的频点占{trusted_ratio:.1f}%。"
            "低相干度频点不可用于判断带宽或谐振。<br>"
            f"输出数据路径：{output_processing}。若固件IIR启用，"
            "Iq实际列是PI看到的滤波反馈，并非未经滤波的原始Park电流。")
