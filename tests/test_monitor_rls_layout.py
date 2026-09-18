from PySide6.QtWidgets import (
    QApplication, QScrollArea, QSizePolicy, QWidget,
)

from communications.comm_manager import CommManager
from pages.monitor_page import MonitorPage


def _valid_sample():
    return {
        "updates": 100,
        "innov_rms_a": 0.02,
        "p_trace": 1200.0,
        "theta_d": (0.94, 0.0, 0.0, 0.08, 0.01, 0.0, 0.0),
        "theta_q": (0.93, 0.0, 0.0, 0.0, 0.0, 0.09, 0.01),
        "a1_d": 0.94,
        "a1_q": 0.93,
        "b_dd0_si": 0.08,
        "b_qq0_si": 0.09,
        "ld_mh": 0.66,
        "lq_mh": 0.67,
        "rd_ohm": 0.59,
        "rq_ohm": 0.61,
    }


def test_RLS曲线在现有页面高度内自适应而不撑高页面():
    page = MonitorPage(CommManager())
    # pyqtgraph 自带约 480 px 的 sizeHint，必须从曲线面板到
    # 标签页都忽略它，不能再把主页面撑高。
    assert page._curve_tabs.sizePolicy().verticalPolicy() == QSizePolicy.Ignored
    assert page.sizeHint().height() < 900

    # 主窗口实际用可缩放滚动容器承载页面。在默认
    # 1280x800 窗口对应的内容区里，RLS 页不得产生纵向滚动。
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(page)
    scroll.resize(1100, 778)
    scroll.show()
    page._curve_tabs.setCurrentIndex(4)
    QApplication.instance().processEvents()

    assert scroll.verticalScrollBar().maximum() == 0
    assert not scroll.verticalScrollBar().isVisible()
    assert page.height() == scroll.viewport().height()
    for curve in (page._c_rls_a1, page._c_rls_L, page._c_rls_R):
        assert curve.maximumHeight() == QWidget().maximumHeight()
        assert curve.sizePolicy().verticalPolicy() == QSizePolicy.Ignored
        assert curve.parentWidget().sizePolicy().verticalPolicy() == QSizePolicy.Ignored
        assert 80 < curve._plot.height() < page._curve_tabs.height()
    scroll.close()


def test_RLS原始系数失效时三组曲线整帧拒绝():
    page = MonitorPage(CommManager())
    sample = _valid_sample()
    sample.update({
        "a1_d": -5.2e11,
        "b_dd0_si": -3.1e11,
        "p_trace": 0.0,
        "theta_d": (-5.2e11, 1.0, 1.0, -3.1e11, 1.0, 0.0, 0.0),
    })

    page._on_rls_coeff(sample)

    assert len(page._c_rls_a1._times) == 0
    assert len(page._c_rls_L._times) == 0
    assert len(page._c_rls_R._times) == 0
    assert "整帧无效" in page._rls_status.text()
    assert "P迹无效" in page._rls_status.text()
    page.close()


def test_RLS有效帧三组曲线同步追加():
    page = MonitorPage(CommManager())

    page._on_rls_coeff(_valid_sample())

    assert len(page._c_rls_a1._times) == 1
    assert len(page._c_rls_L._times) == 1
    assert len(page._c_rls_R._times) == 1
    assert "有效：无" in page._rls_status.text()
    page.close()


def test_RLS三阶曲线显示分母系数和而非误导性单一a1():
    page = MonitorPage(CommManager())
    sample = _valid_sample()
    sample.update({
        "a1_d": 0.50,
        "a1_q": 0.40,
        "theta_d": (0.50, 0.30, 0.14, 0.08, 0.01, 0.0, 0.0),
        "theta_q": (0.40, 0.35, 0.18, 0.0, 0.0, 0.09, 0.01),
    })

    page._on_rls_coeff(sample)

    assert abs(page._c_rls_a1._buffers["Σa_d"][-1] - 0.94) < 1e-12
    assert abs(page._c_rls_a1._buffers["Σa_q"][-1] - 0.93) < 1e-12
    assert "Σa=0.94/0.93" in page._rls_status.text()
    page.close()
