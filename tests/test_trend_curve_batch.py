import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from widgets.trend_curve import TrendCurve


def _app():
    return QApplication.instance() or QApplication([])


def test_高速批次只向弹窗转发一次批量更新():
    _app()
    curve = TrendCurve("current", {"Ia": "#fff"}, buffer_size=5000)
    single_calls = []
    batch_calls = []
    curve.add_popout_callback(single_calls.append)
    curve.add_popout_batch_callback(
        lambda samples, interval: batch_calls.append((samples, interval)))

    samples = [{"Ia": float(index)} for index in range(100)]
    curve.append_batch(samples, 0.001)

    assert single_calls == []
    assert len(batch_calls) == 1
    assert batch_calls[0][0] is samples
    assert batch_calls[0][1] == 0.001
    assert len(curve._times) == 100
    curve.close()
