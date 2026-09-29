"""电流矢量分量分解：把静止坐标系的 i_α + j·i_β 拆成一串旋转矢量，供平面合成动画回放。

两类分量：
  阶次分量 k：c_k · e^{j·k·θe(t)}，按实测电角度锁相（转速变化时仍对得上）
      k=+1 基波正序，0 零偏，−1 负序，−5/+7 死区与谐波，±2 …
  频率分量 f：c_f · e^{j·2π·f·t}，与转速无关的固定频率（如 ±654 Hz 共模干扰）
系数按时间分段（默认 0.25 s）做最小二乘，幅值可随负载、转速缓慢变化。
一对 ±f 分量的合成：幅值相等时沿直线来回摆动，不等时为椭圆。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core.vector_shape import clarke
from core.vbus_legacy import correct_columns

_ORDERS = (1, 0, -1, 2, -2, -5, 7, -11, 13)


@dataclass
class Capture:
    time: np.ndarray                 # s
    ia: np.ndarray                   # A
    ib: np.ndarray
    theta: np.ndarray | None         # 电角度，rad，已展开；无则为 None
    speed_rpm: np.ndarray | None
    rate_hz: float
    source: str
    extra: dict = field(default_factory=dict)   # 其余数值列：iq_a、vd_raw、vq_raw、vbus_v…
    path: Path | None = None

    @property
    def duration(self) -> float:
        return float(self.time[-1] - self.time[0]) if self.time.size else 0.0

    def columns(self) -> dict:
        """功率估算等需要的列（含 ia_a、ib_a、theta）。"""
        data = dict(self.extra)
        data.update({"ia_a": self.ia, "ib_a": self.ib})
        if self.theta is not None:
            data["theta"] = self.theta
        if self.speed_rpm is not None:
            data["speed_rpm"] = self.speed_rpm
        return data

    def window(self, t0: float, t1: float) -> "Capture":
        mask = (self.time >= t0) & (self.time <= t1)
        pick = (lambda a: None if a is None else a[mask])
        return Capture(self.time[mask], self.ia[mask], self.ib[mask], pick(self.theta),
                       pick(self.speed_rpm), self.rate_hz, self.source,
                       {k: v[mask] for k, v in self.extra.items()}, self.path)


@dataclass
class Component:
    key: str
    label: str
    order: int | None                # 阶次分量；频率分量为 None
    freq_hz: float                   # 静止坐标系中的平均频率（带符号）
    coeffs: np.ndarray               # 每段的复系数
    amp: float = 0.0                 # 平均幅值
    dq_freq_hz: float = 0.0          # 在 dq 坐标系中表现出的纹波频率

    @property
    def sequence(self) -> str:
        if abs(self.freq_hz) < 1e-6:
            return "静止"
        return "正序" if self.freq_hz > 0 else "负序"


@dataclass
class Analysis:
    time: np.ndarray                 # 窗内时间，从 t0 开始的绝对时间
    theta: np.ndarray
    z: np.ndarray                    # 实测 i_α + j·i_β
    fe_hz: float                     # 平均电频率（带符号）
    block_edges: np.ndarray          # 分段边界（时间）
    components: list[Component] = field(default_factory=list)
    residual_pct: float = 0.0        # 未解释部分 RMS / 基波幅值

    def block_index(self, t: float) -> int:
        index = int(np.searchsorted(self.block_edges, t, side="right") - 1)
        return min(max(index, 0), len(self.block_edges) - 2)

    def theta_at(self, t: float) -> float:
        return float(np.interp(t, self.time, self.theta))

    def vectors_at(self, t: float, components: list[Component] | None = None) -> list[complex]:
        """t 时刻每个分量的复矢量（与 components 顺序一致）。"""
        block = self.block_index(t)
        theta = self.theta_at(t)
        vectors = []
        for comp in (components if components is not None else self.components):
            phase = comp.order * theta if comp.order is not None else 2 * np.pi * comp.freq_hz * t
            vectors.append(complex(comp.coeffs[block] * np.exp(1j * phase)))
        return vectors

    def series(self, comp: Component, times: np.ndarray) -> np.ndarray:
        """一个分量在一组时刻上的复矢量（向量化，用于画轨迹）。"""
        times = np.asarray(times, float)
        blocks = np.clip(np.searchsorted(self.block_edges, times, side="right") - 1,
                         0, len(self.block_edges) - 2)
        if comp.order is not None:
            phase = comp.order * np.interp(times, self.time, self.theta)
        else:
            phase = 2 * np.pi * comp.freq_hz * times
        return comp.coeffs[blocks] * np.exp(1j * phase)

    def measured_between(self, t0: float, t1: float) -> np.ndarray:
        lo, hi = np.searchsorted(self.time, [t0, t1])
        return self.z[lo:hi + 1]

    def measured_at(self, t: float) -> complex:
        index = min(int(np.searchsorted(self.time, t)), self.time.size - 1)
        return complex(self.z[index])


# ─── 读取记录 ────────────────────────────────────────────────
def load_capture(path: str | Path) -> Capture:
    """读取 高速数据.csv（旧名 RLS辨识数据.csv）这类宽表（time_s, angle_deg, ia_a, ib_a …），或监控页导出的长表。"""
    path = Path(path)
    with open(path, encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream))
    header = [name.strip() for name in header]
    if {"time_s", "ia_a", "ib_a"} <= set(header):
        return _load_wide(path, header)
    if {"channel", "time_s", "series", "value"} <= set(header):
        return _load_long(path)
    raise ValueError("未识别的记录格式：需要 time_s/ia_a/ib_a 宽表，或 channel/series/value 长表")


_EXTRA_COLUMNS = ("iq_a", "iqref_a", "id_a", "idref_a", "vd_raw", "vq_raw", "vbus_v")


def _load_wide(path: Path, header: list[str]) -> Capture:
    wanted = ["time_s", "ia_a", "ib_a"] + [
        c for c in ("angle_deg", "speed_rpm", "vdda_v") + _EXTRA_COLUMNS if c in header]
    columns = [header.index(name) for name in wanted]
    data = np.loadtxt(path, delimiter=",", skiprows=1, usecols=columns,
                      encoding="utf-8-sig", ndmin=2)
    values = dict(zip(wanted, data.T))
    correct_columns(values)          # 旧文件的母线电压按错误分压解码过，读入时还原
    theta = (np.unwrap(np.radians(values["angle_deg"])) if "angle_deg" in values else None)
    capture = _capture(values["time_s"], values["ia_a"], values["ib_a"], theta,
                       values.get("speed_rpm"), f"{path.name}（宽表）")
    capture.extra = {name: values[name] for name in _EXTRA_COLUMNS if name in values}
    capture.path = path
    return capture


def _load_long(path: Path) -> Capture:
    series: dict[str, tuple[list, list]] = {}
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            channel, name = row["channel"].strip(), row["series"].strip()
            if channel == "phase_current" and name in ("Ia", "Ib"):
                key = name
            elif channel == "electrical_angle":
                key = "angle"
            else:
                continue
            times, values = series.setdefault(key, ([], []))
            times.append(float(row["time_s"]))
            values.append(float(row["value"]))
    if "Ia" not in series or "Ib" not in series:
        raise ValueError("长表中没有 phase_current 的 Ia/Ib 列")
    time = np.asarray(series["Ia"][0])
    ia = np.asarray(series["Ia"][1])
    ib = np.interp(time, *map(np.asarray, series["Ib"]))
    theta = None
    if "angle" in series:
        angle_t, angle_v = map(np.asarray, series["angle"])
        theta = np.interp(time, angle_t, np.unwrap(np.radians(angle_v)))
    capture = _capture(time, ia, ib, theta, None, f"{path.name}（长表）")
    capture.path = path
    return capture


def _capture(time, ia, ib, theta, speed, source) -> Capture:
    time = np.asarray(time, float)
    if time.size < 16:
        raise ValueError("记录太短")
    dt = np.median(np.diff(time))
    return Capture(time, np.asarray(ia, float), np.asarray(ib, float), theta,
                   None if speed is None else np.asarray(speed, float),
                   float(1.0 / dt) if dt > 0 else 0.0, source)


def synthetic_capture(params=None, fe_hz: float = 50.0, duration_s: float = 2.0,
                      rate_hz: float = 16000.0, amplitude_a: float = 1.5,
                      common_mode_hz: float = 654.0, common_mode_pct: float = 4.0,
                      noise_pct: float = 0.5, seed: int = 0) -> Capture:
    """演示数据：畸变图谱参数 + 两路采样上的共模干扰（在 αβ 平面沿 60° 直线摆动）。"""
    from core.vector_distortion import DistortionParams, phase_currents
    params = params or DistortionParams()
    time = np.arange(int(duration_s * rate_hz)) / rate_hz
    theta = 2 * np.pi * fe_hz * time
    ia, ib = phase_currents(params, theta)
    common = common_mode_pct / 100.0 * np.cos(2 * np.pi * common_mode_hz * time)
    rng = np.random.default_rng(seed)
    noise = noise_pct / 100.0
    ia = amplitude_a * (ia + common + rng.normal(0, noise, time.size))
    ib = amplitude_a * (ib + common + rng.normal(0, noise, time.size))
    speed = np.full(time.size, fe_hz * 60.0 / 4.0)
    return Capture(time, ia, ib, theta, speed, rate_hz, "演示数据（畸变图谱参数 + 654 Hz 共模干扰）")


# ─── 分解 ────────────────────────────────────────────────────
def _order_label(k: int) -> str:
    names = {1: "基波（正序）", 0: "零偏（静止矢量）", -1: "负序（三相不对称）"}
    return names.get(k, f"{k:+d} 次谐波")


def _freq_label(f: float) -> str:
    return f"{f:+.1f} Hz（{'正序' if f > 0 else '负序'}，与转速无关）"


def _basis(comp_orders, comp_freqs, t, theta):
    cols = [np.exp(1j * k * theta) for k in comp_orders]
    cols += [np.exp(2j * np.pi * f * t) for f in comp_freqs]
    return np.column_stack(cols) if cols else np.zeros((t.size, 0), complex)


def _fit(z, basis):
    coeffs, *_ = np.linalg.lstsq(basis, z, rcond=None)
    return coeffs


def _residual_peaks(residual, rate, exclude_hz, count, min_amp):
    """残差双边频谱中的最大峰（抛物线插值细化频率），避开已有分量附近。"""
    n = residual.size
    window = np.hanning(n)
    spectrum = np.fft.fftshift(np.fft.fft(residual * window)) / (window.sum())
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / rate))
    mag = np.abs(spectrum)
    resolution = rate / n
    guard = max(3.0 * resolution, 2.0)
    peaks = []
    order = np.argsort(mag)[::-1]
    for index in order:
        if len(peaks) >= count or mag[index] < min_amp:
            break
        if index == 0 or index == n - 1 or mag[index] < mag[index - 1] or mag[index] < mag[index + 1]:
            continue
        a, b, c = mag[index - 1], mag[index], mag[index + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom else 0.0
        f = float(freqs[index] + shift * resolution)
        if any(abs(f - g) < guard for g in list(exclude_hz) + peaks):
            continue
        peaks.append(f)
    return peaks


def steady_window(capture: Capture, min_s: float = 0.5,
                  band: float = 0.12) -> tuple[float, float] | None:
    """自动挑出稳态运行段：转速在其中位数 ±band 以内、最长的连续区间。

    启动、停机、停转段会让频谱展宽、让阶次分解失去意义，频谱与矢量分解默认只用稳态段。
    没有转速列时用电角度求电频率；找不到至少 min_s 的稳态段返回 None。
    """
    t = capture.time
    if t.size < 64:
        return None
    if capture.speed_rpm is not None:
        speed = np.asarray(capture.speed_rpm, float)
    elif capture.theta is not None:
        speed = np.gradient(capture.theta, t) / (2 * np.pi) * 60.0
    else:
        return None
    rate = capture.rate_hz or 1.0 / float(np.median(np.diff(t)))
    size = max(1, int(0.05 * rate))                       # 50 ms 滑动平均，压掉测速量化
    smooth = np.convolve(speed, np.ones(size) / size, mode="same")
    peak = float(np.max(np.abs(smooth)))
    if peak < 10.0:
        return None
    running = np.abs(smooth) > 0.3 * peak
    if not running.any():
        return None
    level = float(np.median(smooth[running]))
    inside = np.abs(smooth - level) <= band * max(abs(level), 1.0)
    best, start = (0, 0), None
    for index, flag in enumerate(np.append(inside, False)):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if index - start > best[1] - best[0]:
                best = (start, index)
            start = None
    lo, hi = best
    # 去掉两端各 50 ms，避开平均窗口的边缘
    lo, hi = min(lo + size, hi), max(hi - size, lo)
    if hi - lo < min_s * rate:
        return None
    return float(t[lo]), float(t[hi - 1])


def analyze(capture: Capture, t0: float, t1: float, block_s: float = 0.25,
            max_extra: int = 4, threshold_pct: float = 1.0) -> Analysis:
    mask = (capture.time >= t0) & (capture.time <= t1)
    if mask.sum() < 64:
        raise ValueError("时间窗内的采样点太少")
    t = capture.time[mask]
    alpha, beta = clarke(capture.ia[mask], capture.ib[mask])
    z = alpha + 1j * beta
    if capture.theta is not None:
        theta = capture.theta[mask]
        fe = float(np.polyfit(t - t[0], theta, 1)[0] / (2 * np.pi)) if t.size > 1 else 0.0
    else:
        centered = z - z.mean()
        spectrum = np.abs(np.fft.fft(centered * np.hanning(z.size)))
        freqs = np.fft.fftfreq(z.size, 1.0 / capture.rate_hz)
        fe = float(freqs[int(np.argmax(spectrum))])
        theta = 2 * np.pi * fe * t
    rotating = abs(fe) >= 1.0
    orders = list(_ORDERS) if rotating else [0]

    # 1) 全窗拟合阶次分量，按阈值筛掉小分量（基波与零偏始终保留）
    coeffs = _fit(z, _basis(orders, [], t, theta))
    reference = abs(coeffs[0]) if rotating else max(np.abs(z).mean(), 1e-9)
    keep = [k for k, c in zip(orders, coeffs)
            if k in (1, 0) or abs(c) >= threshold_pct / 100.0 * reference]
    coeffs = _fit(z, _basis(keep, [], t, theta))
    # 2) 残差里找与转速无关的频率峰
    residual = z - _basis(keep, [], t, theta) @ coeffs
    exclude = [k * fe for k in keep]
    extras = _residual_peaks(residual, capture.rate_hz, exclude, max_extra,
                             threshold_pct / 100.0 * reference)
    # 3) 分段拟合：幅值、相位可随时间缓慢变化
    edges = np.arange(t[0], t[-1], max(block_s, 5.0 / capture.rate_hz))
    edges = np.append(edges, t[-1] + 1e-9)
    per_block = []
    width = len(keep) + len(extras)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (t >= lo) & (t < hi)
        if sel.sum() < 2 * width:
            per_block.append(per_block[-1] if per_block else np.zeros(width, complex))
            continue
        span = float(np.ptp(theta[sel])) if sel.any() else 0.0
        if span >= 2 * np.pi:
            per_block.append(_fit(z[sel], _basis(keep, extras, t[sel], theta[sel])))
            continue
        # 本段电角度转不满一圈（停转、启动瞬间）：各阶次 e^{jkθ} 几乎相同、无法区分，
        # 硬拟合会得到巨大且互相抵消的系数。只拟合静止矢量（k=0）与频率分量，其余阶次记 0。
        coeffs = np.zeros(width, complex)
        subset = [i for i, k in enumerate(keep) if k == 0]
        orders = [keep[i] for i in subset]
        partial = _fit(z[sel], _basis(orders, extras, t[sel], theta[sel]))
        for slot, value in zip(subset + list(range(len(keep), width)), partial):
            coeffs[slot] = value
        per_block.append(coeffs)
    block_coeffs = np.array(per_block)                       # [段, 分量]
    fitted = np.empty_like(z)
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        sel = (t >= lo) & (t < hi)
        fitted[sel] = _basis(keep, extras, t[sel], theta[sel]) @ block_coeffs[i]

    components = []
    for index, k in enumerate(keep):
        components.append(Component(f"k{k:+d}", _order_label(k), k, k * fe,
                                    block_coeffs[:, index]))
    for index, f in enumerate(extras, start=len(keep)):
        components.append(Component(f"f{f:+.1f}", _freq_label(f), None, f,
                                    block_coeffs[:, index]))
    for comp in components:
        comp.amp = float(np.mean(np.abs(comp.coeffs)))
        comp.dq_freq_hz = comp.freq_hz - fe
    fundamental = components[0].amp if rotating else 1.0
    # 基波在前，其余按幅值从大到小
    components = components[:1] + sorted(components[1:], key=lambda c: -c.amp) if rotating \
        else sorted(components, key=lambda c: -c.amp)
    residual_pct = float(100.0 * np.sqrt(np.mean(np.abs(z - fitted) ** 2)) / max(fundamental, 1e-9))
    return Analysis(t, theta, z, fe, edges, components, residual_pct)


# ─── 成对分量（直线/椭圆摆动） ─────────────────────────────────
@dataclass(frozen=True)
class PairInfo:
    plus: str
    minus: str
    freq_hz: float
    linearity: float                 # 1 = 纯直线摆动，0 = 圆
    axis_deg: float                  # 摆动方向（αβ 平面，0~180°）


def find_pairs(analysis: Analysis, tolerance_hz: float = 2.0) -> list[PairInfo]:
    """找出频率互为相反数的分量对（不含基波），合成为直线或椭圆摆动。"""
    pairs, used = [], set()
    candidates = [c for c in analysis.components if c.order != 1 and abs(c.freq_hz) > 1e-6]
    for comp in candidates:
        if comp.key in used or comp.freq_hz <= 0:
            continue
        match = min((o for o in candidates if o.key not in used and o.freq_hz < 0),
                    key=lambda o: abs(o.freq_hz + comp.freq_hz), default=None)
        if match is None or abs(match.freq_hz + comp.freq_hz) > max(tolerance_hz, 0.01 * comp.freq_hz):
            continue
        cp, cm = np.mean(comp.coeffs), np.mean(match.coeffs)
        major, minor = abs(cp) + abs(cm), abs(abs(cp) - abs(cm))
        axis = (np.degrees(np.angle(cp) + np.angle(cm)) / 2.0) % 180.0
        pairs.append(PairInfo(comp.key, match.key, comp.freq_hz,
                              1.0 - minor / major if major > 0 else 0.0, float(axis)))
        used.update((comp.key, match.key))
    return pairs
