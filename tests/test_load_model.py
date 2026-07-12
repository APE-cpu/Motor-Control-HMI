"""控制页负载模型测试：负载类型 → 外部负载转矩。"""
import pytest

from pages.control_page import ControlPage


def test_空载恒为零():
    assert ControlPage._compute_load(0, 5.0, 3000) == 0.0


def test_恒转矩不随转速变():
    assert ControlPage._compute_load(1, 0.3, 500) == 0.3
    assert ControlPage._compute_load(1, 0.3, 3000) == 0.3


def test_风机泵类正比转速平方():
    # value = 1000rpm 处负载
    assert ControlPage._compute_load(2, 0.4, 1000) == pytest.approx(0.4)
    assert ControlPage._compute_load(2, 0.4, 2000) == pytest.approx(1.6)  # ×4
    assert ControlPage._compute_load(2, 0.4, 0) == 0.0


def test_对拖可为负():
    assert ControlPage._compute_load(3, -0.2, 1500) == -0.2   # 助力/回馈
