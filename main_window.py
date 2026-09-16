"""主窗口：左侧导航 + 右侧 QStackedWidget。"""
from PySide6.QtCore import (
    Qt, QParallelAnimationGroup, QPropertyAnimation, QEasingCurve,
)
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
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
from communications.comm_manager import CommManager, decode_motor_fault_code
from logs.operation_logger import logger
from core import RuntimeStateMachine
from core import RuntimeState
from config.config import CMD_RESET_FAULT, CMD_START, CMD_STOP

APP_VERSION = "1.9.0"

_STOP_REASON_TEXT = {
    0: "未记录",
    1: "收到上位机STOP",
    2: "收到紧急停机",
    3: "控制链路15秒看门狗",
    4: "电流环测试转速上限",
    5: "跑飞/超速保护",
    6: "控制链路断开回调",
    7: "MCSDK故障",
    8: "固件外部或未归因停机",
}


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
        self._device_stop_confirm_frames = 0
        self._last_fault_history_code = 0
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
        self.monitor_page = MonitorPage(
            self.comm_manager, self.control_page, self.runtime_state)
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

        self.nav.currentIndexChanged.connect(self._switch_page)
        self.nav.select_page(0)

        # 状态栏显示连接状态
        bar = QStatusBar()
        self.setStatusBar(bar)
        self.comm_manager.statusChanged.connect(
            lambda ok, msg: bar.showMessage(f"通信：{'已连接' if ok else '未连接'} - {msg}")
        )
        bar.showMessage("通信：未连接")
        logger.log("软件启动")

    # ---------- 页面切换过渡：旧页左滑淡出 + 新页淡入 ----------
    _TRANSITION_MS = 190

    def _clear_page_transition(self) -> None:
        """停止并拆除页面过渡，保证窗口关闭时不残留 Qt 动画回调。"""
        for name in ("_page_transition_group", "_page_fade_in"):
            animation = getattr(self, name, None)
            if animation is not None:
                animation.stop()
                animation.deleteLater()
                setattr(self, name, None)

        fade_page = getattr(self, "_page_fade_widget", None)
        if fade_page is not None:
            try:
                fade_page.setGraphicsEffect(None)
            except RuntimeError:
                pass
            self._page_fade_widget = None

        overlay = getattr(self, "_page_overlay", None)
        if overlay is not None:
            try:
                overlay.setGraphicsEffect(None)
                overlay.deleteLater()
            except RuntimeError:
                pass
            self._page_overlay = None

    def _switch_page(self, index: int) -> None:
        if index == self.stack.currentIndex():
            return
        # 未显示的窗口（启动装配和无界面测试）无需创建截图、特效与动画；
        # 直接切页也避免在窗口从未进入事件循环时留下原生图形效果对象。
        if not self.isVisible():
            self.stack.setCurrentIndex(index)
            return
        # 快速连点时先完整清理上一场未完成的过渡。
        self._clear_page_transition()

        # ResponsiveStack 对外返回内容页；这里需要直接取得实际承载的
        # QScrollArea，确保截图和淡入效果覆盖完整页面。
        old_page = QStackedWidget.currentWidget(self.stack)
        pixmap = old_page.grab() if old_page is not None else None

        overlay = None
        if pixmap is not None and not pixmap.isNull():
            overlay = QLabel(self.stack)
            overlay.setPixmap(pixmap)
            overlay.setScaledContents(True)
            overlay.setGeometry(self.stack.rect())
            overlay.setAttribute(Qt.WA_TransparentForMouseEvents)
            overlay.show()
            overlay.raise_()
            self._page_overlay = overlay

        self.stack.setCurrentIndex(index)

        if overlay is not None:
            effect = QGraphicsOpacityEffect(overlay)
            overlay.setGraphicsEffect(effect)
            rect = self.stack.rect()
            slide = QPropertyAnimation(overlay, b"geometry", self)
            slide.setDuration(self._TRANSITION_MS)
            slide.setStartValue(rect)
            slide.setEndValue(rect.translated(-28, 0))
            slide.setEasingCurve(QEasingCurve.OutCubic)
            fade = QPropertyAnimation(effect, b"opacity", self)
            fade.setDuration(self._TRANSITION_MS)
            fade.setStartValue(1.0)
            fade.setEndValue(0.0)
            group = QParallelAnimationGroup(self)
            group.addAnimation(slide)
            group.addAnimation(fade)

            def _cleanup(ov=overlay):
                if getattr(self, "_page_overlay", None) is ov:
                    self._page_overlay = None
                ov.deleteLater()
                self._page_transition_group = None
                group.deleteLater()

            group.finished.connect(_cleanup)
            self._page_transition_group = group
            group.start()

        # 新页淡入，结束后摘掉 effect，避免常驻影响重绘性能。
        new_page = QStackedWidget.currentWidget(self.stack)
        if new_page is not None:
            effect = QGraphicsOpacityEffect(new_page)
            new_page.setGraphicsEffect(effect)
            fade_in = QPropertyAnimation(effect, b"opacity", self)
            fade_in.setDuration(self._TRANSITION_MS)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            self._page_fade_widget = new_page
            self._page_fade_in = fade_in

            def _finish_fade(pg=new_page, animation=fade_in):
                try:
                    pg.setGraphicsEffect(None)
                except RuntimeError:
                    pass
                if getattr(self, "_page_fade_in", None) is animation:
                    self._page_fade_in = None
                    self._page_fade_widget = None
                animation.deleteLater()

            fade_in.finished.connect(_finish_fade)
            fade_in.start()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self._clear_page_transition()
        self.nav.stop_animations()
        self.monitor_page.stop_visual_animations()
        super().closeEvent(event)

    def _on_runtime_telemetry(self, frame) -> None:
        """同步下位机主动受控停机 / 保护锁存，避免界面残留在 RUNNING。"""
        mc_state = int(getattr(frame, "mc_state", 0) or 0)
        fault_code = int(getattr(frame, "fault_code", 0) or 0)
        fault_text = str(getattr(frame, "fault_text", "") or "").strip()
        fault_history_code = int(
            getattr(frame, "fault_history_code", 0) or 0)
        fault_history_text = str(
            getattr(frame, "fault_history_text", "") or "").strip()
        stop_reason = int(getattr(frame, "stop_reason", 0) or 0)
        stop_command = int(getattr(frame, "stop_command", 0) or 0)
        stop_rx_age_ms = int(getattr(frame, "stop_rx_age_ms", 0) or 0)
        stop_run_ms = int(getattr(frame, "stop_run_ms", 0) or 0)
        speed_actual = abs(float(getattr(frame, "speed_actual", 0.0) or 0.0))
        speed_target = abs(float(getattr(frame, "speed_target", 0.0) or 0.0))
        is_legacy = self.comm_manager.protocol_status().get("mode") == "legacy-v1"

        if (fault_history_code != 0 and
                fault_history_code != self._last_fault_history_code):
            detail = (fault_history_text or
                      decode_motor_fault_code(fault_history_code))
            logger.log(
                "下位机历史故障锁存",
                f"code=0x{fault_history_code:04X}；{detail}")
        self._last_fault_history_code = fault_history_code

        # 下位机保护锁存（跑飞/反转/通信看门狗/MCSDK）优先于 mc_state==RUN 的早退。
        # 旧逻辑在 mc_state 仍为 RUN 时直接 return，导致保护已触发而 UI 仍显示 running。
        if (fault_code != 0 and
                self.runtime_state.state in (RuntimeState.RUNNING,
                                              RuntimeState.STOPPING)):
            self._device_run_seen = False
            self._device_stop_confirm_frames = 0
            reason = fault_text or f"下位机故障位 0x{fault_code:X}"
            try:
                self.runtime_state.lock_fault(reason)
            except Exception:
                pass
            return

        if self.runtime_state.state is RuntimeState.RUNNING and mc_state == 6:
            self._device_run_seen = True
            self._device_stop_confirm_frames = 0
            return
        if (is_legacy and self.runtime_state.state is RuntimeState.RUNNING and
                (speed_actual >= 30 or speed_target >= 1)):
            self._device_run_seen = True
            self._device_stop_confirm_frames = 0
            return
        if (self._device_run_seen and
                self.runtime_state.state is RuntimeState.RUNNING and
                mc_state in (7, 8, 9, 20)):
            detail = (
                f"{_STOP_REASON_TEXT.get(stop_reason, f'未知原因码{stop_reason}')}；"
                f"mc_state={mc_state}，命令=0x{stop_command:02X}，"
                f"停机时RX空窗={stop_rx_age_ms} ms，运行={stop_run_ms} ms"
            )
            self.runtime_state.observe_device_stopping(detail)
            logger.log("下位机进入停机流程", detail)
            self._device_stop_confirm_frames = 0
            return
        stopped_evidence = (
            self._device_run_seen and
            self.runtime_state.state in (RuntimeState.RUNNING,
                                          RuntimeState.STOPPING) and
            fault_code == 0 and mc_state == 0 and
            speed_actual < 30 and speed_target < 1
        )
        if stopped_evidence:
            self._device_stop_confirm_frames += 1
        else:
            self._device_stop_confirm_frames = 0
        # 单帧 MCSDK 瞬态不再被误判为停机；必须连续三帧同时满足
        # MCSDK已回到IDLE、零目标、近零速、无故障，才允许恢复READY。
        if self._device_stop_confirm_frames >= 3:
            self._device_run_seen = False
            self._device_stop_confirm_frames = 0
            self.runtime_state.observe_device_stopped(
                f"连续遥测确认设备已停机（mc_state={mc_state}，"
                f"目标={speed_target:.1f} rpm，转速={speed_actual:.1f} rpm，"
                f"原因={_STOP_REASON_TEXT.get(stop_reason, stop_reason)}）")

    def _on_v2_command_result(self, result) -> None:
        """用设备ACK/NACK推进状态；超时按设备状态未知处理。"""
        try:
            if result.command == CMD_START:
                if result.success and self.runtime_state.state is RuntimeState.READY:
                    self._device_run_seen = False
                    self._device_stop_confirm_frames = 0
                    self.runtime_state.confirm_started(
                        f"设备ACK启动 SEQ={result.sequence}")
                elif not result.success:
                    reason = (f"设备NACK启动 SEQ={result.sequence} "
                              f"CODE={result.error_code} "
                              f"{result.message}")
                    if result.error_code == -1:
                        # START 超时不能证明设备没有启动，状态未知必须锁定，
                        # 禁止把它伪装成 READY 后直接重试。
                        self.runtime_state.lock_fault(
                            "启动命令ACK超时，设备是否运行未知")
                    else:
                        self._device_run_seen = False
                        self._device_stop_confirm_frames = 0
                        self.runtime_state.reject_start(reason)
                    logger.log(
                        "设备拒绝启动",
                        f"SEQ={result.sequence} CODE={result.error_code} "
                        f"{result.message}")
                    self.statusBar().showMessage(
                        f"启动失败：{result.message or result.error_code}", 15000)
                return
            if result.command == CMD_STOP:
                if (result.success and self.runtime_state.state in
                        (RuntimeState.RUNNING, RuntimeState.STOPPING)):
                    # MC_StopMotor1是非阻塞命令；ACK仅代表设备接受了停机请求。
                    # 必须继续等待mc_state==IDLE，不能在ANY_STOP/STOP时伪装READY。
                    self._device_run_seen = True
                    if self.runtime_state.state is RuntimeState.RUNNING:
                        self.runtime_state.observe_device_stopping(
                            f"设备ACK接受停机 SEQ={result.sequence}")
                    logger.log("设备接受停机命令", f"SEQ={result.sequence}，等待IDLE")
                elif result.error_code == -1:
                    self.runtime_state.lock_fault("停止命令ACK超时，设备状态未知")
                elif self.runtime_state.state is RuntimeState.STOPPING:
                    self.runtime_state.reject_stop(
                        f"设备NACK停机：{result.message or result.error_code}")
                return
            if result.command == CMD_RESET_FAULT:
                if result.success:
                    # 允许下一次同类保护再次触发 faultDetected（否则 key 被记住后 UI 不再锁定）。
                    clear = getattr(self.comm_manager, "clear_reported_faults", None)
                    if callable(clear):
                        clear()
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
