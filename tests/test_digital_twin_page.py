import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QGroupBox, QPushButton

from communications.comm_manager import CommManager
from core import RuntimeState, RuntimeStateMachine
from pages.control_page import ControlPage
from pages.digital_twin_page import DigitalTwinPage
from pages.monitor_page import MonitorPage
from simulink_host import (
    SimulinkEngineThread,
    SimulinkHostInputs,
    SimulinkHostParameters,
)


def _app():
    return QApplication.instance() or QApplication([])


class _FakeRunningEngine:
    def __init__(self, *, stops: bool = True) -> None:
        self.running = True
        self.stops = stops
        self.stop_calls = 0
        self.wait_calls = []

    def isRunning(self) -> bool:  # noqa: N802 - mirrors QThread
        return self.running

    def stop_model(self) -> None:
        self.stop_calls += 1

    def wait(self, timeout_ms: int) -> bool:
        self.wait_calls.append(timeout_ms)
        if self.stops:
            self.running = False
        return self.stops


def test_模型显示邮箱只保留最新帧(tmp_path):
    engine = SimulinkEngineThread(
        tmp_path / "unused.dll",
        SimulinkHostInputs(),
        SimulinkHostParameters(),
    )

    engine._store_latest_sample({"sequence": 1})
    engine._store_latest_sample({"sequence": 2})

    assert engine.take_latest_sample() == {"sequence": 2}
    assert engine.take_latest_sample() is None


def test_数字孪生页面独立运行环境和模型(tmp_path, monkeypatch):
    _app()
    fake_dll = tmp_path / "pmsm_simulink_host.dll"
    fake_dll.write_bytes(b"test")
    monkeypatch.setattr(
        "pages.digital_twin_page.resolve_simulink_dll", lambda: fake_dll)
    comm = CommManager()
    machine = RuntimeStateMachine()
    page = DigitalTwinPage(comm, machine)

    assert page._start_environment() is True
    assert page._environment_ready
    assert not comm.is_sim_running()
    assert machine.state is RuntimeState.CONNECTED

    page._target_speed.setValue(800)
    machine.begin_precheck("测试")
    machine.pass_precheck("测试")
    page._on_engine_state_changed("running")
    assert machine.state is RuntimeState.RUNNING

    machine.request_stop("测试")
    page._on_engine_state_changed("stopped")
    assert machine.state is RuntimeState.READY

    page._stop_environment()
    assert not page._environment_ready
    assert machine.state is RuntimeState.DISCONNECTED
    page.close()
    page.deleteLater()
    _app().processEvents()


def test_负载机械控件只出现在数字孪生页():
    _app()
    comm = CommManager()
    control = ControlPage(comm)
    twin = DigitalTwinPage(comm)
    monitor = MonitorPage(comm, control)

    control_groups = {box.title() for box in control.findChildren(QGroupBox)}
    twin_groups = {box.title() for box in twin.findChildren(QGroupBox)}
    monitor_buttons = {button.text() for button in monitor.findChildren(QPushButton)}

    assert "负载与机械" not in control_groups
    assert "负载与机械（Simulink C++）" in twin_groups
    assert "Simulink C++ 仿真运行" in twin_groups
    assert "启动数字孪生" not in monitor_buttons
    assert "快速仿真演示" not in monitor_buttons
    assert "AI 运行报告" not in monitor_buttons

    control.close()
    twin.close()
    monitor.close()
    control.deleteLater()
    twin.deleteLater()
    monitor.deleteLater()
    _app().processEvents()


def test_实验快照通过只读接口记录仿真负载():
    _app()
    comm = CommManager()
    control = ControlPage(comm)
    twin = DigitalTwinPage(comm)
    control.set_simulation_snapshot_provider(twin.mechanical_snapshot)

    twin._load_type.setCurrentIndex(1)
    twin._load_value.setValue(0.35)
    snapshot = control.experiment_snapshot()

    assert snapshot["controller_params"]["mechanical_load"]["load_value"] == 0.35
    control.close()
    twin.close()
    control.deleteLater()
    twin.deleteLater()
    _app().processEvents()


def test_运行中关闭数字孪生页会先等待模型线程退出():
    _app()
    page = DigitalTwinPage(CommManager())
    engine = _FakeRunningEngine()
    page._engine = engine
    page._engine_state = "running"

    assert page.close() is True
    assert engine.stop_calls == 1
    assert engine.wait_calls == [3000]
    assert engine.running is False
    assert page._engine is None
    page.deleteLater()
    _app().processEvents()


def test_模型线程退出超时时拒绝销毁页面():
    _app()
    page = DigitalTwinPage(CommManager())
    engine = _FakeRunningEngine(stops=False)
    page._engine = engine
    page._engine_state = "running"

    assert page.close() is False
    assert engine.stop_calls == 1
    assert engine.wait_calls == [3000]
    assert page._engine is engine
    engine.running = False
    page.close()
    page.deleteLater()
    _app().processEvents()
