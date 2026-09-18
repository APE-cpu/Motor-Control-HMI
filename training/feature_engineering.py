"""电机遥测滑窗特征工程与可解释性分析。

设计目标：
- 原始点序列按时间先切分训练/验证段，再分别滑窗，避免重叠窗口跨界泄漏；
- 鲁棒裁剪阈值只由训练段拟合；
- 同时生成时域与频域统计特征；
- 特征重要性、相关性和分布分析只把训练段用于“学习型”统计。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np


_EPS = 1e-12


@dataclass(frozen=True)
class FeatureEngineeringConfig:
    sample_rate_hz: float = 10.0
    window_size: int = 128
    hop_size: int = 64
    include_time: bool = True
    include_frequency: bool = True
    robust_clip: bool = True
    clip_lower_quantile: float = 0.005
    clip_upper_quantile: float = 0.995
    deduplicate_adjacent: bool = False
    label_mode: str = "max"
    train_fraction: float = 0.8


@dataclass
class FeatureEngineeringResult:
    X: np.ndarray
    y: np.ndarray
    feature_names: list[str]
    split_index: int
    input_rows: int
    valid_rows: int
    dropped_rows: int
    clipped_values: int
    train_windows: int
    validation_windows: int
    config: FeatureEngineeringConfig
    preprocessing: dict = field(default_factory=dict)


@dataclass
class FeatureAnalysis:
    importance: np.ndarray
    importance_method: str
    target_correlation: np.ndarray
    correlation: np.ndarray
    top_indices: np.ndarray
    summary: list[dict]


def _safe_moment(centered: np.ndarray, std: float, order: int) -> float:
    if std <= _EPS:
        return 0.0
    return float(np.mean((centered / std) ** order))


def _time_features(values: np.ndarray, sample_rate_hz: float) -> tuple[list[float], list[str]]:
    values = np.asarray(values, dtype=np.float64)
    mean = float(np.mean(values))
    centered = values - mean
    std = float(np.std(values))
    rms = float(np.sqrt(np.mean(values * values)))
    abs_values = np.abs(values)
    abs_mean = float(np.mean(abs_values))
    peak = float(np.max(abs_values))
    root_abs_mean = float(np.mean(np.sqrt(abs_values)))
    crossings = np.count_nonzero(centered[:-1] * centered[1:] < 0.0)
    t = np.arange(values.size, dtype=np.float64) / max(sample_rate_hz, _EPS)
    t_centered = t - np.mean(t)
    slope_denom = float(np.dot(t_centered, t_centered))
    slope = (float(np.dot(t_centered, centered)) / slope_denom
             if slope_denom > _EPS else 0.0)

    names = [
        "均值", "标准差", "时域RMS", "最小值", "最大值", "峰峰值",
        "绝对均值", "中位数", "偏度", "峭度", "峰值因子",
        "波形因子", "脉冲因子", "裕度因子", "过均值零交叉率", "趋势斜率",
    ]
    features = [
        mean,
        std,
        rms,
        float(np.min(values)),
        float(np.max(values)),
        float(np.ptp(values)),
        abs_mean,
        float(np.median(values)),
        _safe_moment(centered, std, 3),
        _safe_moment(centered, std, 4),
        peak / max(rms, _EPS),
        rms / max(abs_mean, _EPS),
        peak / max(abs_mean, _EPS),
        peak / max(root_abs_mean * root_abs_mean, _EPS),
        float(crossings / max(1, values.size - 1)),
        slope,
    ]
    return features, names


def _frequency_features(values: np.ndarray, sample_rate_hz: float) -> tuple[list[float], list[str]]:
    values = np.asarray(values, dtype=np.float64)
    n = values.size
    centered = values - np.mean(values)
    window = np.hanning(n) if n > 2 else np.ones(n)
    spectrum = np.fft.rfft(centered * window)
    freqs = np.fft.rfftfreq(n, d=1.0 / max(sample_rate_hz, _EPS))
    power = np.abs(spectrum) ** 2
    if power.size:
        power[0] = 0.0
    total = float(np.sum(power))
    normalized = power / max(total, _EPS)
    dominant_index = int(np.argmax(power)) if total > _EPS else 0
    centroid = float(np.sum(freqs * normalized)) if total > _EPS else 0.0
    rms_frequency = (
        float(np.sqrt(np.sum(freqs * freqs * normalized)))
        if total > _EPS else 0.0)
    bandwidth = (
        float(np.sqrt(np.sum((freqs - centroid) ** 2 * normalized)))
        if total > _EPS else 0.0)
    active = normalized[normalized > _EPS]
    entropy = (
        float(-np.sum(active * np.log(active)) /
              np.log(max(2, normalized.size)))
        if active.size else 0.0)
    nyquist = sample_rate_hz / 2.0

    def band_ratio(low: float, high: float, include_high: bool = False) -> float:
        upper = freqs <= high if include_high else freqs < high
        mask = (freqs >= low) & upper
        return float(np.sum(power[mask]) / max(total, _EPS))

    # Parseval意义下的交流RMS；rFFT除直流/Nyquist外需补齐负频率能量，
    # 再按窗函数能量归一化，正弦输入应与时域RMS一致。
    window_energy = float(np.sum(window * window))
    if power.size <= 1:
        two_sided_energy = 0.0
    elif n % 2 == 0:
        two_sided_energy = float(
            2.0 * np.sum(power[1:-1]) + power[-1])
    else:
        two_sided_energy = float(2.0 * np.sum(power[1:]))
    spectral_rms = float(np.sqrt(
        two_sided_energy / max(n * window_energy, _EPS)))
    names = [
        "频域RMS", "主频_Hz", "频谱质心_Hz", "均方根频率_Hz",
        "频谱带宽_Hz", "频谱熵", "低频能量比", "中频能量比", "高频能量比",
    ]
    features = [
        spectral_rms,
        float(freqs[dominant_index]) if freqs.size else 0.0,
        centroid,
        rms_frequency,
        bandwidth,
        entropy,
        band_ratio(0.0, 0.1 * nyquist),
        band_ratio(0.1 * nyquist, 0.3 * nyquist),
        band_ratio(0.3 * nyquist, nyquist, include_high=True),
    ]
    return features, names


def _aggregate_label(labels: np.ndarray, mode: str) -> float:
    if mode == "mean":
        return float(np.mean(labels))
    if mode == "majority":
        values, counts = np.unique(labels, return_counts=True)
        return float(values[int(np.argmax(counts))])
    return float(np.max(labels))


def _window_features(window: np.ndarray, base_names: Sequence[str],
                     config: FeatureEngineeringConfig) -> tuple[list[float], list[str]]:
    output: list[float] = []
    names: list[str] = []
    for column, base_name in enumerate(base_names):
        values = window[:, column]
        if config.include_time:
            feature_values, feature_names = _time_features(
                values, config.sample_rate_hz)
            output.extend(feature_values)
            names.extend(f"{base_name}·{name}" for name in feature_names)
        if config.include_frequency:
            feature_values, feature_names = _frequency_features(
                values, config.sample_rate_hz)
            output.extend(feature_values)
            names.extend(f"{base_name}·{name}" for name in feature_names)
    return output, names


def _segment_windows(X: np.ndarray, y: np.ndarray, start: int, stop: int,
                     base_names: Sequence[str], config: FeatureEngineeringConfig,
                     expected_names: list[str] | None = None
                     ) -> tuple[list[list[float]], list[float], list[str]]:
    rows: list[list[float]] = []
    labels: list[float] = []
    names = expected_names or []
    last_start = stop - config.window_size
    if last_start < start:
        return rows, labels, names
    for offset in range(start, last_start + 1, config.hop_size):
        values, generated_names = _window_features(
            X[offset:offset + config.window_size], base_names, config)
        if not names:
            names = generated_names
        rows.append(values)
        labels.append(_aggregate_label(
            y[offset:offset + config.window_size], config.label_mode))
    return rows, labels, names


def engineer_features(X: np.ndarray, y: np.ndarray,
                      base_feature_names: Sequence[str],
                      config: FeatureEngineeringConfig
                      ) -> FeatureEngineeringResult:
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64).reshape(-1)
    if X.ndim != 2 or X.shape[0] != y.size:
        raise ValueError("X必须为二维数组，且X/y样本数一致")
    if X.shape[1] != len(base_feature_names):
        raise ValueError("基础特征名称数量与X列数不一致")
    if config.window_size < 4 or config.hop_size < 1:
        raise ValueError("窗口至少4点，步长至少1点")
    if not (config.include_time or config.include_frequency):
        raise ValueError("时域和频域特征至少启用一类")
    if not (0.5 <= config.train_fraction < 1.0):
        raise ValueError("训练段比例必须在[0.5, 1.0)内")

    input_rows = X.shape[0]
    finite = np.isfinite(X).all(axis=1) & np.isfinite(y)
    X, y = X[finite], y[finite]
    if config.deduplicate_adjacent and len(X) > 1:
        changed = np.ones(len(X), dtype=bool)
        changed[1:] = np.any(X[1:] != X[:-1], axis=1) | (y[1:] != y[:-1])
        X, y = X[changed], y[changed]
    valid_rows = X.shape[0]
    if valid_rows < config.window_size * 2:
        raise ValueError(
            f"有效数据只有{valid_rows}点；至少需要两个{config.window_size}点窗口")

    split_row = int(valid_rows * config.train_fraction)
    split_row = min(max(config.window_size, split_row),
                    valid_rows - config.window_size)
    clipped_values = 0
    clip_lower = np.full(X.shape[1], -np.inf)
    clip_upper = np.full(X.shape[1], np.inf)
    if config.robust_clip:
        train_source = X[:split_row]
        clip_lower = np.quantile(
            train_source, config.clip_lower_quantile, axis=0)
        clip_upper = np.quantile(
            train_source, config.clip_upper_quantile, axis=0)
        before = X.copy()
        X = np.clip(X, clip_lower, clip_upper)
        clipped_values = int(np.count_nonzero(before != X))

    train_rows, train_labels, names = _segment_windows(
        X, y, 0, split_row, base_feature_names, config)
    val_rows, val_labels, names = _segment_windows(
        X, y, split_row, valid_rows, base_feature_names, config, names)
    if not train_rows or not val_rows:
        raise ValueError(
            "训练段或验证段不足一个完整窗口；请减小窗口或增加数据")
    combined = np.asarray(train_rows + val_rows, dtype=np.float32)
    labels = np.asarray(train_labels + val_labels, dtype=np.float32)
    return FeatureEngineeringResult(
        X=combined,
        y=labels,
        feature_names=list(names),
        split_index=len(train_rows),
        input_rows=input_rows,
        valid_rows=valid_rows,
        dropped_rows=input_rows - valid_rows,
        clipped_values=clipped_values,
        train_windows=len(train_rows),
        validation_windows=len(val_rows),
        config=config,
        preprocessing={
            "clip_lower": clip_lower.tolist() if config.robust_clip else None,
            "clip_upper": clip_upper.tolist() if config.robust_clip else None,
            "split_raw_row": split_row,
            "base_feature_names": list(base_feature_names),
        },
    )


def _column_correlation(X: np.ndarray, target: np.ndarray) -> np.ndarray:
    output = np.zeros(X.shape[1], dtype=np.float64)
    target_std = float(np.std(target))
    if target_std <= _EPS:
        return output
    for index in range(X.shape[1]):
        column = X[:, index]
        if float(np.std(column)) > _EPS:
            output[index] = float(np.corrcoef(column, target)[0, 1])
    return np.nan_to_num(output)


def _correlation_matrix(X: np.ndarray) -> np.ndarray:
    """Pearson相关矩阵；常量列按0相关处理，避免NaN和运行时警告。"""
    X = np.asarray(X, dtype=np.float64)
    centered = X - np.mean(X, axis=0, keepdims=True)
    norms = np.sqrt(np.sum(centered * centered, axis=0))
    denominator = np.outer(norms, norms)
    correlation = np.divide(
        centered.T @ centered,
        denominator,
        out=np.zeros_like(denominator),
        where=denominator > _EPS,
    )
    variable = norms > _EPS
    correlation[np.diag_indices_from(correlation)] = variable.astype(float)
    return np.clip(correlation, -1.0, 1.0)


def analyze_features(result: FeatureEngineeringResult,
                     top_n: int = 18) -> FeatureAnalysis:
    X = np.asarray(result.X, dtype=np.float64)
    y = np.asarray(result.y, dtype=np.float64)
    split = min(max(1, result.split_index), len(X))
    X_train, y_train = X[:split], y[:split]
    target_corr = _column_correlation(X_train, y_train)
    importance = np.abs(target_corr)
    method = "|Pearson相关系数|（训练段）"
    if np.unique(y_train).size > 1 and len(X_train) >= 10:
        try:
            from sklearn.ensemble import RandomForestRegressor

            forest = RandomForestRegressor(
                n_estimators=200, max_depth=8, min_samples_leaf=2,
                n_jobs=-1, random_state=42)
            forest.fit(X_train, y_train)
            importance = np.asarray(forest.feature_importances_, dtype=np.float64)
            method = "随机森林MDI（仅训练段，200棵树）"
        except (ImportError, ValueError):
            pass
    elif np.unique(y_train).size <= 1:
        method = "标签只有单一取值，无法计算监督式重要性"
        importance = np.zeros(X.shape[1], dtype=np.float64)

    count = min(max(1, int(top_n)), X.shape[1])
    order = np.argsort(importance)[::-1]
    top_indices = order[:count]
    top_matrix = X_train[:, top_indices]
    if top_matrix.shape[1] == 1:
        correlation = np.array(
            [[1.0 if np.std(top_matrix[:, 0]) > _EPS else 0.0]])
    else:
        correlation = _correlation_matrix(top_matrix)

    summary = []
    for index, name in enumerate(result.feature_names):
        column = X[:, index]
        summary.append({
            "name": name,
            "importance": float(importance[index]),
            "target_correlation": float(target_corr[index]),
            "mean": float(np.mean(column)),
            "std": float(np.std(column)),
            "min": float(np.min(column)),
            "max": float(np.max(column)),
        })
    return FeatureAnalysis(
        importance=importance,
        importance_method=method,
        target_correlation=target_corr,
        correlation=correlation,
        top_indices=top_indices,
        summary=summary,
    )
