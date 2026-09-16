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


def test_列式批次不构造逐样本字典且只重绘一次():
    _app()
    curve = TrendCurve("current", {"Ia": "#fff", "Ib": "#000"},
                       buffer_size=5000)
    column_calls = []
    curve.add_popout_columns_callback(
        lambda columns, interval: column_calls.append((columns, interval)))
    columns = {
        "Ia": [float(index) for index in range(100)],
        "Ib": [-float(index) for index in range(100)],
    }

    curve.append_columns(columns, 0.001)

    assert len(column_calls) == 1
    assert column_calls[0][0] is columns
    assert column_calls[0][1] == 0.001
    assert len(curve._times) == 100
    assert list(curve._buffers["Ia"])[-2:] == [98.0, 99.0]
    assert list(curve._buffers["Ib"])[-2:] == [-98.0, -99.0]
    curve.close()
