"""训练页自动标注测试：按遥测健康度判定 正常/警告/故障。"""
from communications.comm_manager import TelemetryFrame
from pages.training_page.page import _auto_label


def _frame(**kw):
    f = TelemetryFrame()
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def test_正常工况标0():
    assert _auto_label(_frame(temperature=40.0, bus_state="normal",
                              sensor_quality=0.95)) == 0.0


def test_过温故障标1():
    assert _auto_label(_frame(temperature=90.0)) == 1.0


def test_母线过压跳闸标1():
    assert _auto_label(_frame(temperature=40.0, bus_state="ov")) == 1.0


def test_温度偏高告警标0_5():
    assert _auto_label(_frame(temperature=70.0, bus_state="normal")) == 0.5


def test_欠压与制动斩波告警():
    assert _auto_label(_frame(temperature=40.0, bus_state="uv")) == 0.5
    assert _auto_label(_frame(temperature=40.0, bus_state="brake")) == 0.5


def test_传感器质量低告警():
    assert _auto_label(_frame(temperature=40.0, sensor_quality=0.3)) == 0.5


def test_低速不可用告警():
    assert _auto_label(_frame(temperature=40.0, low_speed_warn=True)) == 0.5


def test_故障优先级高于告警():
    # 同时过温(故障)且欠压(告警)，应判故障
    assert _auto_label(_frame(temperature=90.0, bus_state="uv")) == 1.0
