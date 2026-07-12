"""主窗口：左侧导航 + 右侧 QStackedWidget。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
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
from pages.operation_log_page import OperationLogPage
from widgets.side_nav import SideNav
from communications.comm_manager import CommManager
from logs.operation_logger import logger

APP_VERSION = "1.5.0"


class MainWindow(QMainWindow):
    def __init__(self, enable_training: bool = True) -> None:
        super().__init__()
        self.setWindowTitle(f"电机控制上位机 v{APP_VERSION}")
        self.resize(1280, 800)

        # 通信管理器：所有页面共享同一个通信会话
        self.comm_manager = CommManager()

        # 中心容器：左右布局
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 页面索引与 stack.addWidget 顺序一致；导航顺序按功能分组，与之解耦
        log_idx = 9 if enable_training else 8
        ai_section = [("🤖 AI 分析", 6), ("🧠 边缘AI", 7)]
        if enable_training:
            ai_section.append(("🎓 模型训练", 8))
        nav_sections = [
            ("运行控制", [("📊 监控页面", 0), ("🎮 电机控制", 1)]),
            ("分析可视化", [("🌀 矢量可视化", 2), ("⚡ 功率流", 3),
                            ("🔍 参数辨识", 4)]),
            ("AI 智能", ai_section),
            ("系统", [("📡 通信设置", 5), ("📋 操作记录", log_idx)]),
        ]

        self.nav = SideNav(nav_sections)
        self.stack = QStackedWidget()

        self.control_page = ControlPage(self.comm_manager)
        self.monitor_page = MonitorPage(self.comm_manager, self.control_page)
        self.vector_page = VectorPage(self.comm_manager)
        self.power_flow_page = PowerFlowPage(self.comm_manager)
        self.identify_page = IdentifyPage(self.comm_manager)
        self.communication_page = CommunicationPage(self.comm_manager)
        self.ai_page = AIPage(self.comm_manager, monitor_page=self.monitor_page)
        self.edge_ai_page = EdgeAIPage(self.comm_manager)
        self.operation_log_page = OperationLogPage()

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

        self.stack.addWidget(self.operation_log_page)

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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        try:
            self.comm_manager.disconnect()
        except Exception:
            pass
        super().closeEvent(event)
