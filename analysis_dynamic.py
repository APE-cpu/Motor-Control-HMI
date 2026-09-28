"""Offline, bounded time/frequency analysis. No device or GUI dependencies.

STFT reports PSD; Morlet CWT reports L2 wavelet coefficient power (not joules).
Order analysis uses constant angular sampling, retaining the original time axis.
"""
from __future__ import annotations

import numpy as np
from scipy import signal
from scipy.integrate import cumulative_trapezoid

MAX_SAMPLES = 2_000_000


def series(snapshot, interval=None, *, timing="strict", min_samples=32):
    y = np.asarray(snapshot.get("values", ()), dtype=float)
    fs = float(snapshot.get("sample_rate_hz", 0))
    t = np.asarray(snapshot.get("times", ()), dtype=float)
    if y.ndim != 1 or not min_samples <= y.size <= MAX_SAMPLES:
        raise ValueError(f"分析需要 {min_samples}～2,000,000 个样本，请缩小采集范围")
    if t.size != y.size:
        if t.size or not np.isfinite(fs) or fs <= 0:
            raise ValueError("时间列与数据不匹配，且不能安全重建时间轴")
        t = np.arange(y.size) / fs
    if interval is not None:
        keep = (t >= interval[0]) & (t <= interval[1])
        t, y = t[keep], y[keep]
    if y.size < min_samples or not np.all(np.isfinite(y)) or not np.all(np.isfinite(t)):
        raise ValueError("区间过短或含无效值；请选取连续有效片段")
    dt = np.diff(t)
    step = float(np.median(dt))
    if step <= 0 or np.any(dt <= 0):
        raise ValueError("时间戳重复或倒退，请选取时间递增的片段")
    if timing == "strict" and np.max(np.abs(dt-step)) > step * .1:
        raise ValueError("时间戳不均匀或存在丢点，请先选择连续采集片段")
    if timing != "strict" and np.max(dt) > step*8:
        i = int(np.argmax(dt))
        raise ValueError(f"采集在 {t[i]:.6g}～{t[i+1]:.6g} s 中断，请将开始/结束时间设在同一连续段内")
    if timing == "raw":
        return t, y, (len(t)-1)/(t[-1]-t[0])
    # Timestamps are authoritative: declared wire rate may differ from curve rate.
    # Small timestamp jitter is resampled explicitly, never treated as perfectly
    # uniform points. The endpoints and number of samples are preserved.
    uniform = np.linspace(t[0], t[-1], len(t))
    if not np.allclose(t, uniform, rtol=0, atol=step*1e-5):
        y = np.interp(uniform, t, y)
    return uniform, y, (len(t)-1)/(t[-1]-t[0])


def align_reference(snapshot, t, *, angle=False):
    if angle:
        snapshot = dict(snapshot, values=np.rad2deg(np.unwrap(
            np.deg2rad(np.asarray(snapshot.get("values", ()), dtype=float)))))
    rt, ry, _ = series(snapshot, timing="raw", min_samples=2)
    if rt[0] > t[0] + 1e-8 or rt[-1] < t[-1] - 1e-8:
        raise ValueError("参考信号没有覆盖所选时间区间，请缩小区间")
    if angle:
        ry = ry / 360
    return np.interp(t, rt, ry)


def stft_map(t, y, fs, window=512, fmax=None):
    n = min(int(window), len(y))
    if n < 32 or n > 8192:
        raise ValueError("STFT 窗长应为 32～8192 点")
    hop = max(n // 8, int(np.ceil((len(y)-n) / 1000)), 1)
    # For very long captures, sample windows rather than build a giant matrix.
    starts = np.arange(0, len(y)-n+1, hop)
    frames = np.lib.stride_tricks.sliding_window_view(y, n)[starts]
    win = signal.windows.hann(n, sym=False)
    z = np.fft.rfft((frames-frames.mean(axis=1, keepdims=True))*win, axis=1)
    p = abs(z)**2 / (fs*np.sum(win**2))
    p[:, 1:(-1 if n % 2 == 0 else None)] *= 2
    f = np.fft.rfftfreq(n, 1/fs)
    keep = f <= (fs/2 if fmax is None else min(fmax, fs/2))
    return dict(time=t[0]+(starts+n/2)/fs, axis=f[keep], power=p[:, keep].T,
                resolution=fs/n, window_s=n/fs, unit="PSD", kind="STFT")


def cwt_map(t, y, fs, fmin, fmax, cancel=lambda: False):
    if not 0 < fmin < fmax <= fs/2:
        raise ValueError("小波频率范围应满足 0 < 下限 < 上限 ≤ 奈奎斯特频率")
    # FFT convolution one scale at a time; bounded input, no scales × raw-N array.
    if len(y) > 262144:
        raise ValueError("小波分析最多 262,144 点，请缩小时间区间")
    frequencies = np.linspace(fmin, fmax, 80)
    indices = np.unique(np.linspace(0, len(y)-1, min(1000, len(y))).astype(int))
    power = np.empty((len(frequencies), len(indices)))
    centered = y - y.mean()
    for j, freq in enumerate(frequencies):
        if cancel():
            raise InterruptedError("分析已取消")
        scale = 6*fs/(2*np.pi*freq)
        half = min(int(np.ceil(5*scale)), len(y)-1)
        u = np.arange(-half, half+1)/scale
        wavelet = np.pi**(-.25)/np.sqrt(scale) * (
            np.exp(6j*u)-np.exp(-18)) * np.exp(-u*u/2)
        c = signal.fftconvolve(centered, wavelet.conj()[::-1], mode="same")
        power[j] = abs(c[indices])**2
        # Morlet e-folding cone of influence; invalid edge values stay masked.
        edge = np.sqrt(2)*scale
        power[j, (indices < edge) | (indices > len(y)-1-edge)] = np.nan
    return dict(time=t[indices], axis=frequencies, power=power,
                kind="Morlet CWT", unit="|W|²（L2，小波系数功率）")


def order_map(t, y, fs, reference, reference_kind="speed", max_order=10,
              turns_per_window=4):
    if reference_kind == "speed":
        rpm = align_reference(reference, t)
        cycles = cumulative_trapezoid(rpm/60, t, initial=0)
    else:
        cycles = align_reference(reference, t, angle=True)
    delta = np.diff(cycles)
    direction = np.sign(np.median(delta))
    if direction == 0 or np.any(delta*direction <= 1e-12):
        raise ValueError("阶次分析需要持续单向旋转，请排除停转、反转或角度分辨率不足的区间")
    cycles = (cycles-cycles[0])*direction
    rev_s = delta*direction*fs
    max_allowed = .45*fs/np.max(rev_s)
    if not 0 < max_order <= max_allowed:
        raise ValueError(f"当前采样率/最高转速允许最大阶次 {max_allowed:.2f}")
    # Angular rate >= the highest original samples/rev: no angular downsampling
    # and therefore no aliasing of high temporal frequencies into low orders.
    spr = int(np.ceil(max(32, fs/np.min(rev_s))))
    count = int(np.floor(cycles[-1]*spr)) + 1
    nwin = int(round(turns_per_window*spr))
    if count > MAX_SAMPLES or nwin > 131072:
        raise ValueError("转速跨度过大或低速段过长，请按转速分段分析")
    if nwin < 32 or count < nwin:
        raise ValueError(f"所选区间不足 {turns_per_window:g} 转，请扩大区间或减小窗长")
    angle = np.arange(count)/spr
    vals = np.interp(angle, cycles, y)
    hop = max(nwin//8, int(np.ceil((count-nwin)/600)), 1)
    starts = np.arange(0, count-nwin+1, hop)
    win = signal.windows.hann(nwin, sym=False)
    # Bound memory even for high samples/revolution: process windows separately.
    orders = np.fft.rfftfreq(nwin, 1/spr)
    keep = orders <= max_order
    rows = []
    for start in starts:
        frame = vals[start:start+nwin]
        z = np.fft.rfft((frame-frame.mean())*win)
        rows.append((2*abs(z[keep])/win.sum()/np.sqrt(2))**2)
    centers = (starts+nwin/2)/spr
    return dict(time=np.interp(centers, cycles, t), axis=orders[keep],
                power=np.asarray(rows).T, unit="阶次 RMS²", kind="阶次",
                resolution=spr/nwin, reference=reference_kind,
                rpm=np.interp(centers, cycles[1:], rev_s*60))


def response_metrics(t, y, event, target=None, band=.02, dwell=.05,
                     smooth_s=.01, saturation=None, limit=None):
    before = y[t < event]
    after = t >= event
    if len(before) < 5 or np.count_nonzero(after) < 20:
        raise ValueError("阶跃时刻前至少需要 5 点，之后至少需要 20 点")
    baseline = float(np.median(before[-max(5, len(before)//5):]))
    target = float(np.median(y[-max(5, len(y)//10):])) if target is None else float(target)
    amplitude = target-baseline
    if abs(amplitude) < max(1e-12, np.std(before)*3):
        raise ValueError("阶跃幅度过小或小于前段噪声，请检查目标值及阶跃时刻")
    step = float((t[-1]-t[0])/(len(t)-1))
    width = max(5, int(round(smooth_s/step)) | 1)
    width = min(width, (len(y)-1) | 1)
    uniform = np.linspace(t[0], t[-1], len(t))
    regular_y = np.interp(uniform, t, y)
    filtered = np.interp(t, uniform, signal.savgol_filter(regular_y, width, 2))
    derivative = np.interp(t, uniform, signal.savgol_filter(
        regular_y, width, 2, deriv=1, delta=step))
    ta, ya = t[after], y[after]
    normalized = (ya-baseline)/amplitude
    def crossing(level):
        hits = np.flatnonzero(normalized >= level)
        if not len(hits):
            return None
        i = int(hits[0])
        if not i:
            return float(ta[0])
        return float(np.interp(level, normalized[i-1:i+1], ta[i-1:i+1]))
    t10, t90 = crossing(.1), crossing(.9)
    outside = np.flatnonzero(abs(ya-target) > abs(amplitude)*band)
    first = int(outside[-1]+1) if len(outside) else 0
    settling = (float(ta[first]-event) if first < len(ta) and
                ta[-1]-ta[first] >= dwell else None)
    residual = (filtered[after]-target)/amplitude
    peaks, _ = signal.find_peaks(residual, prominence=.02)
    troughs, _ = signal.find_peaks(-residual, prominence=.02)
    # Count alternating lobes across the target, not arbitrary noisy extrema.
    peaks = peaks[residual[peaks] > .01]
    troughs = troughs[residual[troughs] < -.01]
    ring_hz = None
    if len(peaks) >= 2 and any(peaks[0] < k < peaks[-1] for k in troughs):
        ring_hz = float(1/np.median(np.diff(ta[peaks])))
    sat = None
    if saturation is not None:
        sat = np.asarray(saturation, dtype=bool)
        if sat.shape != y.shape:
            raise ValueError("饱和状态数据未对齐")
    elif limit is not None:
        if limit <= 0:
            raise ValueError("限幅值必须为正")
        sat = np.abs(y) >= limit
    # Time-weighted occupation on irregular timestamps; the unobserved interval
    # after the final sample is never included in saturation duration.
    durations = np.maximum(0., t[1:]-np.maximum(t[:-1], event))
    sat_time = None if sat is None else float(np.sum(durations*sat[:-1]))
    metrics = dict(baseline=baseline, target=target,
                   rise_s=None if t10 is None or t90 is None else t90-t10,
                   overshoot_pct=max(0., float(normalized.max()-1))*100,
                   settling_s=settling,
                   steady_error=float(np.mean(ya[-max(5, len(ya)//10):])-target),
                   peak_slope=float(np.max(abs(derivative[after]))),
                   ringing_hz=ring_hz,
                   saturation_fraction=None if sat is None else sat_time/np.sum(durations),
                   saturation_s=sat_time)
    return dict(metrics=metrics, time=t, values=y, filtered=filtered,
                derivative=derivative, saturation=sat, event=event, band=band,
                t10=t10, t90=t90, dwell_s=dwell, smooth_s=width*step)


def demo_snapshot():
    fs = 4000.
    t = np.arange(8000)/fs
    phase = 2*np.pi*(30*t+15*t*t)
    ring = np.where(t >= 1, .75*np.exp(-np.maximum(t-1, 0)/.16)*np.sin(2*np.pi*250*t), 0)
    common = dict(times=t, sample_rate_hz=fs)
    return (dict(common, values=np.cos(phase)+.25*np.cos(2*phase)+ring, unit="A"),
            dict(common, values=(30+30*t)*60, unit="rpm"))
