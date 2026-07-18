"""主窗口：左侧导航 + 右侧 QStackedWidget。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QFrame,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from pages.monitor_page import MonitorPage
from pages.control_page import ControlPage
from pages.communication_page import CommunicationPage
from pages.ai_page import AIPage
from pages.edge_ai_page import EdgeAIPage
from pages.identify_page import IdentifyPage
from pages.vector_page import VectorPage
from pages.power_flow_page import PowerFlowPage
from pages.manual_page import ManualPage
from pages.operation_log_page import OperationLogPage
from pages.experiment_page import ExperimentPage
from pages.current_sampling_page import CurrentSamplingPage
from widgets.side_nav import SideNav
from communications.comm_manager import CommManager
from logs.operation_logger import logger
from core import RuntimeStateMachine
from core import RuntimeState
from config.config import CMD_RESET_FAULT, CMD_START, CMD_STOP

APP_VERSION = "1.7.0"


class ResponsiveStack(QStackedWidget):
    """主页面滚动容器：小屏幕不压扁控件，而是出现滚动条。"""

    def __init__(self) -> None:
        super().__init__()
        self._content_pages: list[QWidget] = []

    def addWidget(self, page: QWidget) -> int:  # noqa: N802 - Qt API
        page.setMinimumWidth(980)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(page)
        index = super().addWidget(scroll)
        self._content_pages.append(page)
        return index

    def indexOf(self, widget: QWidget) -> int:  # noqa: N802 - Qt API
        if widget in self._content_pages:
            return self._content_pages.index(widget)
        return super().indexOf(widget)

    def currentWidget(self) -> QWidget | None:  # noqa: N802 - Qt API
        current = super().currentWidget()
        return current.widget() if isinstance(current, QScrollArea) else current

    def widget(self, index: int) -> QWidget | None:
        if 0 <= index < len(self._content_pages):
            return self._content_pages[index]
        return None


class MainWindow(QMainWindow):
    def __init__(self, enable_training: bool = True) -> None:
        super().__init__()
        self.setWindowTitle(f"电机控制上位机 v{APP_VERSION}")
        self.resize(1280, 800)

        # 通信管理器：所有页面共享同一个通信会话
        self.comm_manager = CommManager()
        self.runtime_state = RuntimeStateMachine()
        self._device_run_seen = False
        self.comm_manager.statusChanged.connect(self.runtime_state.connection_changed)
        self.comm_manager.faultDetected.connect(self.runtime_state.lock_fault)
        self.comm_manager.commandResult.connect(self._on_v2_command_result)
        self.comm_manager.telemetryReceived.connect(self._on_runtime_telemetry)
        self.runtime_state.stateChanged.connect(
            lambda previous, current, reason: logger.log(
                "运行状态切换", f"{previous.value} → {current.value}：{reason}"))

        # 中心容器：左右布局
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 页面索引与 stack.addWidget 顺序一致；导航顺序按功能分组，与之解耦
        sampling_idx = 9 if enable_training else 8
        experiment_idx = sampling_idx + 1
        log_idx = experiment_idx + 1
        manual_idx = log_idx + 1
        ai_section = [("🤖 AI 分析", 6), ("🧠 边缘AI", 7)]
        if enable_training:
            ai_section.append(("🎓 模型训练", 8))
        nav_sections = [
            ("运行控制", [("📊 监控页面", 0), ("🎮 电机控制", 1),
                          ("🧪 实验管理", experiment_idx)]),
            ("分析可视化", [("🌀 矢量可视化", 2), ("⚡ 功率流", 3),
                            ("🔍 参数辨识", 4), ("🔬 电流采样诊断", sampling_idx)]),
            ("AI 智能", ai_section),
            ("系统", [("📡 通信设置", 5), ("📋 操作记录", log_idx),
                    ("📖 使用说明书", manual_idx)]),
        ]

        self.nav = SideNav(nav_sections)
        self.stack = ResponsiveStack()

        self.control_page = ControlPage(self.comm_manager, self.runtime_state)
        self.monitor_page = MonitorPage(self.comm_manager, self.control_page)
        self.vector_page = VectorPage(self.comm_manager)
        self.power_flow_page = PowerFlowPage(self.comm_manager)
        self.identify_page = IdentifyPage(self.comm_manager)
        self.communication_page = CommunicationPage(self.comm_manager)
        self.ai_page = AIPage(self.comm_manager, monitor_page=self.monitor_page)
        self.edge_ai_page = EdgeAIPage(self.comm_manager)
        self.operation_log_page = OperationLogPage()
        self.manual_page = ManualPage()
        self.experiment_page = ExperimentPage(
            self.comm_manager, software_version=APP_VERSION,
            snapshot_provider=self.control_page.experiment_snapshot,
            runtime_state=self.runtime_state)
        self.current_sampling_page = CurrentSamplingPage(self.comm_manager)

        self.stack.addWidget(self.monitor_page)
        self.stack.addWidget(self.control_page)
        self.stack.addWidget(self.vector_page)
        self.stack.addWidget(self.power_flow_page)
        self.stack.addWidget(self.identify_page)
        self.stack.addWidget(self.communication_page)
        self.stack.addWidget(self.ai_page)
        self.stack.addWidget(self.edge_ai_page)

        if enable_training:
            from pages.training_page import TrainingPage
            self.training_page = TrainingPage(self.comm_manager, self.control_page)
            self.stack.addWidget(self.training_page)

        self.stack.addWidget(self.current_sampling_page)
        self.stack.addWidget(self.experiment_page)
        self.stack.addWidget(self.operation_log_page)
        self.stack.addWidget(self.manual_page)

        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.nav.currentIndexChanged.connect(self.stack.setCurrentIndex)
        self.nav.select_page(0)

        # 状态栏显示连接状态
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.comm_manager.statusChanged.connect(
            lambda ok, msg: bar.showMessage(f"通信：{'已连接' if ok else '未连接'} - {msg}")
        )
        bar.showMessage("通信：未连接")
        logger.log("软件启动")

    def _on_runtime_telemetry(self, frame) -> None:
        """同步下位机主动受控停机，避免界面残留在 RUNNING。"""
        mc_state = int(getattr(frame, "mc_state", 0) or 0)
        if self.runtime_state.state is RuntimeState.RUNNING and mc_state == 6:
            self._device_run_seen = True
            return
        if (self._device_run_seen and
                self.runtime_state.state in (RuntimeState.RUNNING,
                                              RuntimeState.STOPPING) and
                int(getattr(frame, "fault_code", 0) or 0) == 0 and
                mc_state not in (6, 11)):
            self._device_run_seen = False
            self.runtime_state.observe_device_stopped(
                "下位机达到调试安全边界并受控停机")

    def _on_v2_command_result(self, result) -> None:
        """用设备ACK/NACK推进状态；超时按设备状态未知处理。"""
        try:
            if result.command == CMD_START:
                if result.success and self.runtime_state.state is RuntimeState.READY:
                    self._device_run_seen = False
                    self.runtime_state.confirm_started(
                        f"设备ACK启动 SEQ={result.sequence}")
                elif not result.success:
                    logger.log(
                        "设备拒绝启动",
                        f"SEQ={result.sequence} CODE={result.error_code} "
                        f"{result.message}")
                return
            if result.command == CMD_STOP and \
                    self.runtime_state.state is RuntimeState.STOPPING:
                if result.success:
                    self.runtime_state.confirm_stopped(
                        f"设备ACK停机 SEQ={result.sequence}")
                elif result.error_code == -1:
                    self.runtime_state.lock_fault("停止命令ACK超时，设备状态未知")
                else:
                    self.runtime_state.reject_stop(
                        f"设备NACK停机：{result.message or result.error_code}")
                return
            if result.command == CMD_RESET_FAULT:
                if result.success:
                    if self.runtime_state.state is RuntimeState.FAULT_LOCKED:
                        self.runtime_state.reset_fault(
                            f"设备ACK故障复位 SEQ={result.sequence}")
                    logger.log("设备确认故障复位", f"SEQ={result.sequence}")
                else:
                    logger.log(
                        "设备拒绝故障复位",
                        f"SEQ={result.sequence} CODE={result.error_code} "
                        f"{result.message}")
        except Exception as exc:
            logger.log("运行状态应答处理失败", str(exc))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self.experiment_page.shutdown()
        try:
            self.comm_manager.disconnect()
        except Exception:
            pass
        super().closeEvent(event)
