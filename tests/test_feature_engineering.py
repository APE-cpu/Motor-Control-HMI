import numpy as np
import pytest

from pages.training_page.feature_analysis import FeatureAnalysisPanel
from training.feature_engineering import (
    FeatureEngineeringConfig, analyze_features, engineer_features,
)
from training.trainer import Trainer


def _signal_dataset(sample_rate=1000.0, count=4000):
    time = np.arange(count, dtype=float) / sample_rate
    current = 2.0 * np.sin(2.0 * np.pi * 50.0 * time)
    temperature = 30.0 + 0.5 * np.sin(2.0 * np.pi * 2.0 * time)
    labels = np.zeros(count, dtype=float)
    labels[1200:1800] = 0.5
    labels[2400:] = 1.0
    return np.column_stack([current, temperature]), labels


def test_滑窗同时生成时域RMS和频域主频且训练验证不跨界():
    X, y = _signal_dataset()
    result = engineer_features(
        X, y, ["电流", "温度"],
        FeatureEngineeringConfig(
            sample_rate_hz=1000.0, window_size=256, hop_size=128,
            train_fraction=0.75, robust_clip=False),
    )

    rms_index = result.feature_names.index("电流·时域RMS")
    spectral_rms_index = result.feature_names.index("电流·频域RMS")
    frequency_index = result.feature_names.index("电流·主频_Hz")
    assert result.X[0, rms_index] == pytest.approx(np.sqrt(2.0), rel=0.02)
    assert result.X[0, spectral_rms_index] == pytest.approx(
        np.sqrt(2.0), rel=0.02)
    assert result.X[0, frequency_index] == pytest.approx(50.0, abs=2.0)
    assert result.split_index == result.train_windows
    assert result.validation_windows > 0
    assert np.all(result.y[result.split_index:] == 1.0)


def test_鲁棒裁剪阈值只由训练段拟合():
    train = np.linspace(0.0, 1.0, 800)
    validation = np.full(200, 100.0)
    X = np.concatenate([train, validation]).reshape(-1, 1)
    y = np.r_[np.zeros(800), np.ones(200)]
    result = engineer_features(
        X, y, ["信号"],
        FeatureEngineeringConfig(
            sample_rate_hz=100.0, window_size=64, hop_size=32,
            train_fraction=0.8, robust_clip=True),
    )

    assert result.preprocessing["clip_upper"][0] < 2.0
    assert result.clipped_values >= 200
    maximum_index = result.feature_names.index("信号·最大值")
    assert np.max(result.X[:, maximum_index]) < 2.0


def test_分析结果包含重要性相关性和稳定的常量列处理():
    X, y = _signal_dataset(count=5000)
    result = engineer_features(
        X, y, ["电流", "温度"],
        FeatureEngineeringConfig(
            sample_rate_hz=1000.0, window_size=128, hop_size=64,
            train_fraction=0.8, robust_clip=False),
    )
    analysis = analyze_features(result, top_n=12)

    assert analysis.importance.shape == (result.X.shape[1],)
    assert analysis.correlation.shape == (12, 12)
    assert np.isfinite(analysis.correlation).all()
    assert len(analysis.summary) == result.X.shape[1]


def test_Zscore仅用训练段拟合而不是读取验证集():
    trainer = Trainer()
    X = np.array([[0.0], [1.0], [2.0], [3.0], [100.0], [200.0]],
                 dtype=np.float32)
    y = np.zeros(6, dtype=np.float32)

    X_train, _y_train, X_val, _y_val = trainer._split_and_scale(
        X, y, val_split=0.2, normalize=True, split_index=4)

    assert trainer._input_mean[0] == pytest.approx(1.5)
    assert np.mean(X_train[:, 0]) == pytest.approx(0.0, abs=1e-6)
    assert X_val[0, 0] > 50.0


def test_特征分析面板可以呈现统计图表():
    X, y = _signal_dataset(count=5000)
    result = engineer_features(
        X, y, ["电流", "温度"],
        FeatureEngineeringConfig(
            sample_rate_hz=1000.0, window_size=128, hop_size=64,
            train_fraction=0.8, robust_clip=False),
    )
    analysis = analyze_features(result, top_n=10)
    panel = FeatureAnalysisPanel()

    panel.set_result(result, analysis)

    assert panel.summary_table.rowCount() == result.X.shape[1]
    assert panel.correlation_table.rowCount() == 10
    assert panel.distribution_feature.count() == 10
    assert "生成" in panel.status.text()
    panel.close()
