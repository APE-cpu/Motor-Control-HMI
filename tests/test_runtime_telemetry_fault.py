import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communications.comm_manager import TelemetryFrame
from core import RuntimeState, RuntimeStateMachine
from main_window import MainWindow


def _app():
    return QApplication.instance() or QApplication([])


def test_保护故障码立即离开RUNNING():
    _app()
    # Avoid full MainWindow UI construction cost where possible — still need wiring.
    win = MainWindow(enable_training=False)
    rs = win.runtime_state
    rs.connection_changed(True, "t")
    rs.begin_precheck()
    rs.pass_precheck()
    rs.confirm_started("t")
    assert rs.state is RuntimeState.RUNNING

    frame = TelemetryFrame()
    frame.mc_state = 6  # still RUN briefly after latch
    frame.fault_code = 0x8000
    frame.fault_text = "V2 runaway/reverse-speed trip"
    frame.speed_actual = -200.0
    frame.speed_target = 1000.0
    win._device_run_seen = True
    win._on_runtime_telemetry(frame)

    assert rs.state is RuntimeState.FAULT_LOCKED
    assert "runaway" in rs.history[-1].reason.lower() or "0x8000" in rs.history[-1].reason or "V2" in rs.history[-1].reason
