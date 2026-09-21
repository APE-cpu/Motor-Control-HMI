import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager
from pages.identify_page import IdentifyPage


def _app():
    return QApplication.instance() or QApplication([])


def test_参数辨识页保留机械流程并新增电气CSV交叉校验入口():
    _app()
    page = IdentifyPage(CommManager())

    assert page._btn_run.text() == "开始辨识实验"
    assert page._btn_electrical_csv.text() == "导入 RLS 辨识 CSV…"
    assert page._btn_electrical_help.text() == "公式与可辨识性"
    initial = page._electrical_report.toPlainText()
    assert "dq电压方程拟合" in initial
    assert "独立PRBS" in initial


def test_电气辨识报告明确区分物理结果和ARX不可辨识磁链():
    dataset = {"count": 16000, "rate_hz": 16000}
    analysis = {
        "results": [{
            "theta_d": (0.9, 0, 0, 0.05, 0, 0, 0),
            "theta_q": (0.9, 0, 0, 0, 0, 0.05, 0),
        }],
        "physical_iv": {"verdict": "probe_required"},
        "legacy_evidence": {
            "verdict": "diagnostic_only",
            "resistance_ohm_median": 0.539,
            "resistance_ohm_range": (0.46, 0.59),
            "shared_inductance_mh_median": 0.913,
            "shared_inductance_mh_range": (0.83, 0.94),
            "flux_wb_median": 0.00732,
            "flux_wb_range": (0.00688, 0.00801),
        },
        "arx_motor_projection": {
            "d_axis": {"valid": True, "resistance_ohm": 2.0,
                       "inductance_mh": 20.0, "reasons": []},
            "q_axis": {"valid": True, "resistance_ohm": 2.0,
                       "inductance_mh": 20.0, "reasons": []},
        },
        "arx_motor_cross_check": {
            "reference_source": "legacy_quasi_steady_voltage_equation",
            "comparisons": {
                "d_axis": {"comparable": True,
                           "resistance_relative_error": 2.71,
                           "inductance_relative_error": 20.91,
                           "matches_tolerance": False},
                "q_axis": {"comparable": True,
                           "resistance_relative_error": 2.71,
                           "inductance_relative_error": 20.91,
                           "matches_tolerance": False},
            },
            "rl_match": False,
        },
    }

    text = IdentifyPage._format_electrical_analysis(dataset, analysis)

    assert "R  = 0.539 Ω" in text
    assert "ψf = 7.32 mWb" in text
    assert "ψf_ARX = 不可辨识" in text
    assert "三参数完整校验: 不可能" in text
