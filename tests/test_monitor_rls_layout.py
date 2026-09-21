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


def test_RLS原始系数出现NaN时三组曲线整帧拒绝():
    page = MonitorPage(CommManager())
    sample = _valid_sample()
    sample.update({
        "a1_d": float("nan"),
        "b_dd0_si": float("nan"),
        "p_trace": float("nan"),
        "theta_d": (float("nan"), 1.0, 1.0, -3.1e11, 1.0, 0.0, 0.0),
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
    assert "模型同构数值有效（不代表物理R/L收敛）：ARX摘要可计算" in page._rls_status.text()
    assert "#ffb74d" in page._rls_status.styleSheet()
    page.close()


def test_RLS更新计数不变时明示保持而非假装收敛():
    page = MonitorPage(CommManager())
    page._on_rls_coeff(_valid_sample())
    page._on_rls_coeff(_valid_sample())

    assert "递推：保持" in page._rls_status.text()
    assert "未收到新递推" in page._rls_status.text()
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


def test_RLS不再把数值投影到旧物理边界():
    page = MonitorPage(CommManager())
    sample = _valid_sample()
    sample.update({"ld_mh": 0.05, "lq_mh": 0.0501254})

    page._on_rls_coeff(sample)

    assert "约束饱和" not in page._rls_status.text()
    assert "Lnom=0.66mH/ωo=4000/1拍电压延迟→ARX(3)" in \
        page._rls_status.text()
    assert "#ffb74d" in page._rls_status.styleSheet()
    assert len(page._c_rls_a1._times) == 1
    assert len(page._c_rls_L._times) == 1
    assert len(page._c_rls_R._times) == 1
    assert page._c_rls_L._buffers["b0_d"][-1] == 0.08
    assert "不代表物理R/L收敛" in page._rls_status.text()
    page.close()


def test_RLS完整系数弹窗保留dq各7条原始波形():
    page = MonitorPage(CommManager())

    page._on_rls_coeff(_valid_sample())

    dialog = page._rls_coeff_dialog
    assert len(dialog._d_curve._buffers) == 7
    assert len(dialog._q_curve._buffers) == 7
    assert all(len(values) == 1 for values in dialog._d_curve._buffers.values())
    assert all(len(values) == 1 for values in dialog._q_curve._buffers.values())
    page._show_rls_coefficients()
    assert dialog.isVisible()
    dialog.close()
    page.close()


def test_离线辨识入口位于RLS标签页且不增加主页高度():
    page = MonitorPage(CommManager())

    assert page._btn_offline_rls.text() == "离线辨识 CSV…"
    assert page._offline_rls_dialog.isWindow()
    assert "真机匹配ESO" in page._offline_rls_dialog.windowTitle()
    assert "Lnom=0.66mH" in page._offline_rls_dialog._status.text()
    assert "F1/40" in page._offline_rls_dialog._status.text()
    assert set(page._offline_rls_dialog._c_id._buffers) == {
        "原始 id", "ESO id_hat"}
    assert set(page._offline_rls_dialog._c_iq._buffers) == {
        "原始 iq", "ESO iq_hat"}
    assert page.sizeHint().height() < 900
    page.close()


def test_离线RLS数值稳定但激励不足时不得标为物理收敛():
    page = MonitorPage(CommManager())
    dialog = page._offline_rls_dialog
    dialog._complete(
        {"count": 16000, "rate_hz": 16000},
        {
            "results": [_valid_sample()],
            "trace": {},
            "diagnostics": {
                "verdict": "not_identifiable",
                "reasons": ["d轴激励不足", "q轴电流与转速共线"],
            },
        },
        "",
    )

    assert "数值稳定不等于物理收敛" in dialog._status.text()
    assert "d轴激励不足" in dialog._status.text()
    assert "#ffb74d" in dialog._status.styleSheet()
    page.close()
