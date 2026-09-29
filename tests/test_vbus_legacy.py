import numpy as np

from config.config import f1_bus_adc, f1_bus_voltage
from core.vbus_legacy import MAX_DECODABLE_V, correct_columns, is_legacy_vbus


def _legacy_decode(vbus_true: float, vdda: float) -> float:
    """旧版上位机的错误解码：同一个 ADC 读数按 0.027 接地分压换算。"""
    return f1_bus_adc(vbus_true) / 65536.0 * vdda / 0.0270


def test_正确公式与固件F0一致且满量程不超过61V():
    assert abs(f1_bus_voltage(f1_bus_adc(24.0)) - 24.0) < 0.01
    assert 60.0 < MAX_DECODABLE_V < 62.0


def test_旧文件的母线电压和施加电压按比例还原():
    vdda = np.full(5, 3.27)
    old = np.array([_legacy_decode(24.0, 3.27)] * 5)
    assert old[0] > 80.0 and is_legacy_vbus(old)
    columns = {"vbus_v": old, "vdda_v": vdda,
               "vq_applied_v": np.full(5, 7.0), "vd_applied_v": np.full(5, -1.0)}
    assert correct_columns(columns)
    assert np.allclose(columns["vbus_v"], 24.0, atol=0.02)
    ratio = 24.0 / old[0]
    assert np.allclose(columns["vq_applied_v"], 7.0 * ratio, atol=1e-3)
    assert columns["vbus_legacy_corrected"]


def test_新文件不做修正():
    columns = {"vbus_v": np.full(4, 24.0), "vq_applied_v": np.full(4, 2.0)}
    assert not correct_columns(columns)
    assert np.allclose(columns["vbus_v"], 24.0)
