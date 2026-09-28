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
              turns_per_window=4, minimum_rpm=1.):
    """Window within each rotation segment; never span stops or reversals.

    Angular sampling density is chosen per window. Slow startup samples no
    longer inflate the allocation for an entire acceleration record.
    """
    if max_order <= 0 or turns_per_window <= 0 or max_order*turns_per_window > 4096:
        raise ValueError("最高阶次 × 每窗转数需在 0～4096 内")
    if minimum_rpm <= 0:
        raise ValueError("最低有效转速必须为正")
    if reference_kind == "speed":
        rpm = align_reference(reference, t)
        cycles = cumulative_trapezoid(rpm/60, t, initial=0)
    else:
        cycles = align_reference(reference, t, angle=True)
    delta = np.diff(cycles)
    velocity = delta/np.diff(t)
    signs = np.where(abs(velocity)*60 >= minimum_rpm, np.sign(velocity), 0).astype(int)
    if reference_kind == "angle":
        # Bridge only short equal-code plateaus caused by encoder quantization.
        # No opposite-sign increment is bridged; long stationary periods stay 0.
        edges = np.r_[0, np.flatnonzero(np.diff(signs != 0))+1, len(signs)]
        for start, end in zip(edges[:-1], edges[1:]):
            if (signs[start] == 0 and start > 0 and end < len(signs)
                    and signs[start-1] == signs[end] and t[end]-t[start] <= .02):
                signs[start:end] = signs[start-1]
    edges = np.r_[0, np.flatnonzero(np.diff(signs))+1, len(signs)]
    segments, skipped = [], []
    for start, end in zip(edges[:-1], edges[1:]):
        direction = int(signs[start])
        turns = float(abs(cycles[end]-cycles[start]))
        if direction == 0 or turns < turns_per_window or end-start < 31:
            skipped.append(dict(start=float(t[start]), end=float(t[end]),
                                reason="停转/低速" if direction == 0 else "旋转段不足窗长", turns=turns))
        else:
            segments.append((start, end, direction, turns))
    if not segments:
        return dict(time=np.array([t[0], t[-1]]), axis=np.array([0., max_order]),
                    power=np.full((2,2), np.nan), unit="阶次 RMS²", kind="阶次",
                    resolution=1/turns_per_window, reference=reference_kind,
                    segments=[], skipped=skipped, valid_time_ranges=[],
                    message=f"暂无可计算阶次的旋转段；每段至少需要 {turns_per_window:g} 转。原始波形仍可查看。")
    orders = np.arange(int(np.floor(max_order*turns_per_window))+1)/turns_per_window
    count = sum(max(1, int((s[3]-turns_per_window)/(turns_per_window/8))+1) for s in segments)
    window_skip = max(1, int(np.ceil(count/600)))
    rows, centers, speeds, descriptions, ranges = [], [], [], [], []
    window_number = 0
    for first, last, direction, turns in segments:
        angular = (cycles[first:last+1]-cycles[first])*direction
        # Same angle codes add no angular information; keep the last timestamp.
        keep = np.r_[np.diff(angular)>1e-12, True]
        phase, ts, values = angular[keep], t[first:last+1][keep], y[first:last+1][keep]
        rates = np.diff(phase)/np.diff(ts)
        group_times = []
        hop = turns_per_window/8
        frame_count = int(np.floor(max(0, turns-turns_per_window)/hop+1e-9))+1
        offset = (-window_number) % window_skip
        window_number += frame_count
        for index in range(offset, frame_count, window_skip):
            start = index*hop
            left = max(0, np.searchsorted(phase, start, side="right")-1)
            right = min(len(rates), np.searchsorted(phase, start+turns_per_window)+1)
            local = rates[left:right]
            if not len(local) or np.min(local) <= 0:
                continue
            nwin = max(32, int(np.ceil(fs/np.min(local)*turns_per_window)))
            center = float(np.interp(start+turns_per_window/2, phase, ts))
            power = np.full(len(orders), np.nan)
            if nwin <= 131072:
                grid = start + np.arange(nwin)*turns_per_window/nwin
                frame = np.interp(grid, phase, values)
                win = signal.windows.hann(nwin, sym=False)
                z = np.fft.rfft((frame-frame.mean())*win)
                valid = (orders <= .45*fs/np.max(local)) & (np.arange(len(orders)) < len(z))
                power[valid] = (np.sqrt(2)*abs(z[np.flatnonzero(valid)])/win.sum())**2
                power[0] /= 2
            rows.append(power)
            centers.append(center)
            speeds.append(direction*float(np.mean(local))*60)
            group_times.append(center)
        if group_times:
            ranges.append([group_times[0], group_times[-1]])
            descriptions.append(dict(start=float(ts[0]), end=float(ts[-1]), direction=direction,
                                     turns=turns, frames=len(group_times)))
    if not rows:
        raise ValueError("可用旋转段过短，请减小每窗转数")
    return dict(time=np.asarray(centers), axis=orders, power=np.asarray(rows).T,
                unit="阶次 RMS²", kind="阶次", resolution=1/turns_per_window,
                reference=reference_kind, rpm=np.asarray(speeds), segments=descriptions,
                skipped=skipped, valid_time_ranges=ranges,
                message=f"已分段计算 {len(descriptions)} 段旋转；停转/短段及超出采样能力的阶次留空")


def response_metrics(t, y, event, target=None, band=.02, dwell=.05,
                     smooth_s=.01, saturation=None, limit=None):
    before = y[t < event]
    after = t >= event
    enough = len(before) >= 5 and np.count_nonzero(after) >= 20
    base_samples = before[-max(5, len(before)//5):] if len(before) else y[:max(5,len(y)//10)]
    baseline = float(np.median(base_samples))
    target = float(np.median(y[-max(5, len(y)//10):])) if target is None else float(target)
    amplitude = target-baseline
    noise = float(1.4826*np.median(abs(base_samples-np.median(base_samples))))
    step_valid = enough and abs(amplitude) > max(1e-12, noise*3)
    reason = ("" if step_valid else "阶跃前后样本不足，已显示常规动态分析" if not enough else
              "未确认有效阶跃：目标与基线接近或噪声较大；已显示波形和变化率，阶跃指标不适用")
    step = float((t[-1]-t[0])/(len(t)-1))
    width = max(5, int(round(smooth_s/step)) | 1)
    width = min(width, (len(y)-1) | 1)
    uniform = np.linspace(t[0], t[-1], len(t))
    regular_y = np.interp(uniform, t, y)
    filtered = np.interp(t, uniform, signal.savgol_filter(regular_y, width, 2))
    derivative = np.interp(t, uniform, signal.savgol_filter(
        regular_y, width, 2, deriv=1, delta=step))
    evaluate = after if np.any(after) else np.ones(len(t), dtype=bool)
    ta, ya = t[evaluate], y[evaluate]
    normalized = (ya-baseline)/amplitude if step_valid else np.zeros_like(ya)
    def crossing(level):
        hits = np.flatnonzero(normalized >= level)
        if not len(hits):
            return None
        i = int(hits[0])
        if not i:
            return float(ta[0])
        return float(np.interp(level, normalized[i-1:i+1], ta[i-1:i+1]))
    t10, t90 = (crossing(.1), crossing(.9)) if step_valid else (None, None)
    outside = np.flatnonzero(abs(ya-target) > abs(amplitude)*band)
    first = int(outside[-1]+1) if len(outside) else 0
    settling = (float(ta[first]-event) if step_valid and first < len(ta) and
                ta[-1]-ta[first] >= dwell else None)
    residual = (filtered[evaluate]-target)/amplitude if step_valid else np.zeros(len(ta))
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
    durations = np.maximum(0., t[1:]-np.maximum(t[:-1], event if step_valid else t[0]))
    sat_time = None if sat is None else float(np.sum(durations*sat[:-1]))
    metrics = dict(baseline=baseline, target=target,
                   rise_s=None if t10 is None or t90 is None else t90-t10,
                   overshoot_pct=max(0., float(normalized.max()-1))*100 if step_valid else None,
                   settling_s=settling,
                   steady_error=float(np.mean(ya[-max(5, len(ya)//10):])-target) if step_valid else None,
                   peak_slope=float(np.max(abs(derivative[evaluate if step_valid else np.ones(len(t),dtype=bool)]))),
                   ac_rms=float(np.sqrt(np.mean((y-y.mean())**2))),
                   peak_to_peak=float(np.ptp(y)),
                   ringing_hz=ring_hz,
                   saturation_fraction=None if sat is None else sat_time/np.sum(durations),
                   saturation_s=sat_time)
    return dict(metrics=metrics, time=t, values=y, filtered=filtered,
                derivative=derivative, saturation=sat, event=event, band=band,
                t10=t10, t90=t90, dwell_s=dwell, smooth_s=width*step,
                step_valid=step_valid, message=reason)


def suggest_step(t, y, reference=None):
    """Suggest a reference jump, or estimate response onset; never invent one."""
    if reference is not None:
        rt, ry, _ = series(reference, timing="raw", min_samples=2)
        change = abs(np.diff(ry))
        eligible = (rt[1:] >= t[min(5,len(t)-1)]) & (rt[1:] <= t[max(0,len(t)-20)])
        if np.any(eligible):
            index = int(np.argmax(np.where(eligible, change, -1)))
            if change[index] > max(1e-12, np.ptp(ry)*.05):
                return dict(event=float(rt[index+1]), target=float(ry[index+1]), basis="给定信号跳变")
    count = max(5, len(y)//20)
    initial, final = y[:count], y[-count:]
    baseline, target = float(np.median(initial)), float(np.median(final))
    amplitude = target-baseline
    noise = max(float(np.std(initial)),float(np.std(final)))
    if abs(amplitude) <= max(1e-12, 6*noise):
        return None
    hits = np.flatnonzero((y-baseline)/amplitude > max(.02, 3*noise/abs(amplitude)))
    if not len(hits) or hits[0] < 5 or hits[0] > len(t)-20:
        return None
    return dict(event=float(t[max(5,hits[0]-1)]), target=target, basis="响应起点估计（可手动修正）")


def demo_snapshot():
    fs = 4000.
    t = np.arange(8000)/fs
    phase = 2*np.pi*(30*t+15*t*t)
    ring = np.where(t >= 1, .75*np.exp(-np.maximum(t-1, 0)/.16)*np.sin(2*np.pi*250*t), 0)
    common = dict(times=t, sample_rate_hz=fs)
    return (dict(common, values=np.cos(phase)+.25*np.cos(2*phase)+ring, unit="A"),
            dict(common, values=(30+30*t)*60, unit="rpm"))
