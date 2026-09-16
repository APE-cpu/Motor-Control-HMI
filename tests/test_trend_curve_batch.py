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


def test_隐藏曲线只缓存且弹窗回调仍持续接收(monkeypatch):
    _app()
    curve = TrendCurve("current", {"Ia": "#fff"}, buffer_size=5000)
    draw_calls = []
    column_calls = []
    monkeypatch.setattr(curve, "_draw", lambda: draw_calls.append(True))
    curve.add_popout_columns_callback(
        lambda columns, interval: column_calls.append((columns, interval)))

    columns = {"Ia": [1.0, 2.0, 3.0]}
    curve.append_columns(columns, 0.001, redraw=False)

    assert list(curve._buffers["Ia"]) == [1.0, 2.0, 3.0]
    assert draw_calls == []
    assert column_calls == [(columns, 0.001)]

    curve.redraw()
    assert draw_calls == [True]
    curve.close()


def test_统计和量程计算按低频节流(monkeypatch):
    _app()
    curve = TrendCurve("current", {"Ia": "#fff"})
    stats_calls = []
    range_calls = []
    monkeypatch.setattr(curve, "_update_stats",
                        lambda: stats_calls.append(True))
    monkeypatch.setattr(curve, "_update_y_range",
                        lambda: range_calls.append(True))

    curve.append({"Ia": 1.0})
    curve.append({"Ia": 2.0})

    assert len(stats_calls) == 1
    assert len(range_calls) == 1
    curve.redraw()
    assert len(stats_calls) == 2
    assert len(range_calls) == 2
    curve.close()
