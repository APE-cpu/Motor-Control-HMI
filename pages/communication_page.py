"""通信设置页面：通信方式选择、参数配置、连接控制、收发测试与日志。"""
import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

_LOG_PREFIXES = {
    "全部": None,
    "状态": "[状态]",
    "警告": "[警告]",
    "发送": "[发送]",
    "接收": "[接收]",
}

from communications.comm_manager import CommManager
from communications.serial_comm import SerialComm
from config.config import (
    BAUD_RATES_CAN, BAUD_RATES_SERIAL, COMM_TYPES,
    CMD_EMERGENCY_STOP, CMD_SET_PARAMS, CMD_SET_SENSOR, CMD_START, CMD_STOP,
    CMD_TELEMETRY, FRAME_HEADER, FRAME_TAIL,
    PARITY_OPTIONS, STOP_BITS_OPTIONS,
    TELEM_ANGLE_SCALE, TELEM_CURRENT_SCALE, TELEM_FMT, TELEM_FMT_CAN,
    TELEM_LEN, TELEM_LEN_CAN, TELEM_TEMP_OFFSET,
)
from runtime_paths import writable_path

_COMM_CFG_FILE = writable_path("config", "comm_config.json")


def _protocol_doc() -> str:
    """从 config 常量生成下位机对接协议速查文本（常量改动自动同步）。"""
    return f"""\
═══════ 下位机对接协议速查 ═══════
（内容由 config/config.py 常量实时生成；可移植 C 协议栈见仓库 下位机适配/ 目录）

【一、串口帧格式（RS-232 / RS-485 通用）】

  [HEAD] [CMD] [LEN] [PAYLOAD × LEN] [CHKSUM] [TAIL]
   1B     1B    1B      N 字节          1B      1B

  HEAD   = 0x{FRAME_HEADER:02X}
  TAIL   = 0x{FRAME_TAIL:02X}
  CHKSUM = 所有 PAYLOAD 字节求和 & 0xFF（不含头/命令/长度）
  字节序 = 小端（little-endian）

【二、命令字】

  上行（下位机 → 上位机）：
    0x{CMD_TELEMETRY:02X}  遥测帧（payload 格式见下）

  下行（上位机 → 下位机）：
    0x{CMD_START:02X}  启动电机
    0x{CMD_STOP:02X}  停止电机
    0x{CMD_EMERGENCY_STOP:02X}  紧急停止
    0x{CMD_SET_PARAMS:02X}  下发控制参数
    0x{CMD_SET_SENSOR:02X}  下发位置传感器配置

【三、上行遥测 payload（串口，{TELEM_LEN} 字节，struct "{TELEM_FMT}"）】

  偏移  类型    字段            单位/换算
   0    int16   speed_actual    rpm
   2    uint16  speed_target    rpm
   4    int16   current_actual  mA（上位机 ÷{TELEM_CURRENT_SCALE:.0f} → A）
   6    uint16  angle_raw       传感器原始量（Hall 扇区/QEP 计数等）
   8    int16   angle_actual    0.01°（上位机 ÷{TELEM_ANGLE_SCALE:.0f} → °）
  10    int8    temperature     °C，偏置 -{TELEM_TEMP_OFFSET:.0f}（上位机 +{TELEM_TEMP_OFFSET:.0f} 还原）
  11    uint8   sensor_quality  0~255 → 0~1
  12    uint8   convergence     0~255 → 0~1
  13    uint8   flags           bit0 = 低速警告
  （末尾 1 字节 padding，payload 共 {TELEM_LEN} 字节）

  下位机建议发送频率 ≥10 Hz（上位机以 10 Hz 轮询显示）。

【四、CAN 总线】

  遥测上行：8 字节裸 payload（无帧头/校验/帧尾），struct "{TELEM_FMT_CAN}"：
    int16 speed_actual | uint16 speed_target | int16 current_actual(mA) | uint16 angle_raw
  上位机下行默认仲裁 ID = 0x100；旋转变压器遥测默认 ID = 0x201。
  标准帧（11 位 ID），波特率与本页「波特率」设置一致。

【五、以太网 TCP】

  上位机作为监听端（Server）等待下位机接入。
  当前版本 TCP 数据仅原样显示在日志中，不做遥测帧解析。

【六、下位机侧参考实现】

  仓库 下位机适配/ 目录提供可移植 C 协议栈：
    protocol_portable.c/h        —— 串口帧编解码（与本页协议同源）
    can_protocol_portable.c/h    —— CAN 版
    platforms/                   —— STM32 HAL / TI C2000 / Arduino / FPGA 适配示例
"""


class _ProtocolDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("下位机对接说明")
        self.resize(680, 640)
        v = QVBoxLayout(self)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(_protocol_doc())
        text.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        v.addWidget(text, 1)
        h = QHBoxLayout()
        btn_copy = QPushButton("复制全文")
        btn_copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(text.toPlainText())
        )
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        h.addStretch(1)
        h.addWidget(btn_copy)
        h.addWidget(btn_close)
        v.addLayout(h)


class _SerialPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.port = QComboBox(); self.port.setEditable(True)
        for p in SerialComm.list_ports():
            self.port.addItem(p)
        if self.port.count() == 0:
            self.port.addItems(["COM1", "COM2", "COM3", "COM4"])
        self.baud = QComboBox(); self.baud.addItems([str(b) for b in BAUD_RATES_SERIAL]); self.baud.setCurrentText("115200")
        self.databits = QComboBox(); self.databits.addItems(["8"])
        self.stopbits = QComboBox(); self.stopbits.addItems(STOP_BITS_OPTIONS)
        self.parity = QComboBox(); self.parity.addItems(PARITY_OPTIONS)
        self.timeout = QDoubleSpinBox(); self.timeout.setRange(0.0, 60.0); self.timeout.setValue(1.0)
        f.addRow("端口号", self.port)
        f.addRow("波特率", self.baud)
        f.addRow("数据位", self.databits)
        f.addRow("停止位", self.stopbits)
        f.addRow("校验位", self.parity)
        f.addRow("超时时间 (s)", self.timeout)

    def cfg(self) -> dict:
        return {
            "port": self.port.currentText().strip(),
            "baudrate": int(self.baud.currentText()),
            "bytesize": int(self.databits.currentText()),
            "stopbits": self.stopbits.currentText(),
            "parity": self.parity.currentText(),
            "timeout": float(self.timeout.value()),
        }


class _CanPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.channel = QLineEdit("COM9")
        self.bitrate = QComboBox(); self.bitrate.addItems([str(b) for b in BAUD_RATES_CAN])
        self.bitrate.setCurrentText("250000")
        self.interface = QComboBox(); self.interface.addItems(["slcan", "gs_usb", "socketcan", "pcan", "kvaser", "vector", "virtual"])
        f.addRow("CAN 通道", self.channel)
        f.addRow("波特率 (bps)", self.bitrate)
        f.addRow("驱动接口", self.interface)

    def cfg(self) -> dict:
        return {
            "channel": self.channel.text().strip(),
            "bitrate": int(self.bitrate.currentText()),
            "interface": self.interface.currentText(),
        }


class _TcpPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.host = QLineEdit("0.0.0.0")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(8888)
        self.timeout = QDoubleSpinBox(); self.timeout.setRange(1.0, 60.0); self.timeout.setValue(10.0)
        f.addRow("监听地址", self.host)
        f.addRow("监听端口", self.port)
        f.addRow("等待连接超时 (s)", self.timeout)

    def cfg(self) -> dict:
        return {
            "host": self.host.text().strip(),
            "port": self.port.value(),
            "timeout": self.timeout.value(),
        }


class CommunicationPage(QWidget):
    def __init__(self, comm: CommManager) -> None:
        super().__init__()
        self._comm = comm

        root = QVBoxLayout(self)
        title = QLabel("通信设置")
        title.setObjectName("TitleLabel")
        root.addWidget(title)

        # ---- 通信方式 ----
        mode_box = QGroupBox("通信方式")
        mh = QHBoxLayout(mode_box)
        self._kind = QComboBox(); self._kind.addItems(COMM_TYPES)
        self._kind.currentIndexChanged.connect(self._on_kind_changed)
        self._status_label = QLabel("未连接")
        mh.addWidget(QLabel("通信类型："))
        mh.addWidget(self._kind)
        mh.addStretch(1)
        mh.addWidget(QLabel("状态："))
        mh.addWidget(self._status_label)
        root.addWidget(mode_box)

        # ---- 参数面板 ----
        param_box = QGroupBox("通信参数")
        pv = QVBoxLayout(param_box)
        self._stack = QStackedWidget()
        # 232 与 485 在 PC 侧均为串口，但允许用户分别保存参数，因此各自一份面板
        self._serial_panel = _SerialPanel()
        self._serial_panel2 = _SerialPanel()
        self._can_panel = _CanPanel()
        self._tcp_panel = _TcpPanel()
        self._stack.addWidget(self._serial_panel)   # idx 0 -> RS-232
        self._stack.addWidget(self._serial_panel2)  # idx 1 -> RS-485
        self._stack.addWidget(self._can_panel)      # idx 2 -> CAN
        self._stack.addWidget(self._tcp_panel)      # idx 3 -> TCP
        pv.addWidget(self._stack)
        root.addWidget(param_box)

        # ---- 连接控制 ----
        ctrl_h = QHBoxLayout()
        self._btn_connect = QPushButton("连接")
        self._btn_connect.setObjectName("PrimaryButton")
        self._btn_disconnect = QPushButton("断开")
        self._btn_connect.clicked.connect(self._on_connect)
        self._btn_disconnect.clicked.connect(self._on_disconnect)
        btn_save_cfg = QPushButton("保存配置")
        btn_load_cfg = QPushButton("加载配置")
        btn_save_cfg.clicked.connect(self._on_save_cfg)
        btn_load_cfg.clicked.connect(self._on_load_cfg)
        btn_protocol = QPushButton("下位机对接说明")
        btn_protocol.clicked.connect(lambda: _ProtocolDialog(self).exec())
        ctrl_h.addWidget(self._btn_connect)
        ctrl_h.addWidget(self._btn_disconnect)
        ctrl_h.addWidget(btn_save_cfg)
        ctrl_h.addWidget(btn_load_cfg)
        ctrl_h.addWidget(btn_protocol)
        ctrl_h.addStretch(1)
        root.addLayout(ctrl_h)

        # ---- 收发测试 ----
        test_box = QGroupBox("数据收发测试")
        tv = QVBoxLayout(test_box)
        self._send_mode = QComboBox(); self._send_mode.addItems(["十六进制", "文本"])
        self._send_mode.currentIndexChanged.connect(self._on_send_mode_changed)
        self._send_edit = QLineEdit()
        self._send_edit.setPlaceholderText("输入十六进制，例如：AA 10 00 00 55")
        self._btn_send = QPushButton("发送")
        self._btn_send.clicked.connect(self._on_send)
        th = QHBoxLayout()
        th.addWidget(self._send_mode)
        th.addWidget(self._send_edit, 1)
        th.addWidget(self._btn_send)
        tv.addLayout(th)
        root.addWidget(test_box)

        # ---- 日志 ----
        log_box = QGroupBox("通信日志 / 错误信息")
        lv = QVBoxLayout(log_box)
        lh = QHBoxLayout()
        lh.addWidget(QLabel("过滤："))
        self._log_filter = QComboBox(); self._log_filter.addItems(list(_LOG_PREFIXES.keys()))
        self._log_filter.currentIndexChanged.connect(self._apply_log_filter)
        lh.addWidget(self._log_filter); lh.addStretch(1)
        lv.addLayout(lh)
        self._log = QPlainTextEdit(); self._log.setReadOnly(True)
        self._log_lines: list[str] = []
        lv.addWidget(self._log)
        root.addWidget(log_box, 1)

        # ---- 连接信号 ----
        comm.statusChanged.connect(self._on_status)
        comm.logMessage.connect(self._append_log)
        comm.rawReceived.connect(self._on_raw_received)

        # 初始页面 + 自动加载配置
        self._on_kind_changed(0)
        self._on_load_cfg(silent=True)

    # ------- slots -------
    def _on_kind_changed(self, idx: int) -> None:
        # idx: 0=RS-232, 1=RS-485, 2=CAN, 3=TCP
        self._stack.setCurrentIndex(idx)

    def _current_cfg(self) -> dict:
        idx = self._kind.currentIndex()
        if idx == 0:
            return self._serial_panel.cfg()
        if idx == 1:
            return self._serial_panel2.cfg()
        if idx == 2:
            return self._can_panel.cfg()
        return self._tcp_panel.cfg()

    def _on_connect(self) -> None:
        kind = self._kind.currentText()
        cfg = self._current_cfg()
        ok = self._comm.connect(kind, **cfg)
        if not ok:
            QMessageBox.warning(self, "连接失败", "请检查参数或查看日志。")

    def _on_disconnect(self) -> None:
        self._comm.disconnect()

    def _on_save_cfg(self) -> None:
        cfg = {"kind": self._kind.currentText(), "params": self._current_cfg()}
        try:
            with open(_COMM_CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            self._append_log("[状态] 通信配置已保存")
        except Exception as e:
            self._append_log(f"[警告] 保存配置失败：{e}")

    def _on_load_cfg(self, silent: bool = False) -> None:
        try:
            with open(_COMM_CFG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            kind = cfg.get("kind", "")
            idx = self._kind.findText(kind)
            if idx >= 0:
                self._kind.setCurrentIndex(idx)
            params = cfg.get("params", {})
            panel = [self._serial_panel, self._serial_panel2, self._can_panel, self._tcp_panel][self._kind.currentIndex()]
            for key, val in params.items():
                w = getattr(panel, key.split("_")[0] if "_" in key else key, None) or getattr(panel, key, None)
                if w is None:
                    continue
                if hasattr(w, "setValue"):
                    w.setValue(val)
                elif hasattr(w, "setCurrentText"):
                    w.setCurrentText(str(val))
            if not silent:
                self._append_log("[状态] 通信配置已加载")
        except FileNotFoundError:
            pass
        except Exception as e:
            if not silent:
                self._append_log(f"[警告] 加载配置失败：{e}")

    def _on_send_mode_changed(self, idx: int) -> None:
        if idx == 0:
            self._send_edit.setPlaceholderText("输入十六进制，例如：AA 10 00 00 55")
        else:
            self._send_edit.setPlaceholderText("输入文本，例如：hello")

    def _on_send(self) -> None:
        text = self._send_edit.text()
        if self._send_mode.currentIndex() == 1:
            data = text.encode("utf-8")
        else:
            text = text.strip().replace(",", " ").replace("0x", "")
            try:
                data = bytes(int(x, 16) for x in text.split() if x)
            except Exception:
                QMessageBox.warning(self, "格式错误", "请输入空格分隔的十六进制字节")
                return
        if not data:
            return
        self._comm.send_frame(data)

    def _on_status(self, ok: bool, msg: str) -> None:
        self._status_label.setText("已连接" if ok else "未连接")
        self._btn_connect.setEnabled(not ok)
        self._btn_disconnect.setEnabled(ok)
        self._append_log(f"[状态] {msg}")

    def _on_raw_received(self, arb_id: int, data: bytes) -> None:
        for enc in ("utf-8", "gbk"):
            try:
                text = data.decode(enc).rstrip("\x00\r\n")
                self._append_log(f"[接收] {text}  ({data.hex(' ').upper()})")
                return
            except Exception:
                pass
        self._append_log(f"[接收] ID=0x{arb_id:03X} {data.hex(' ').upper()}")

    def _append_log(self, line: str) -> None:
        self._log_lines.append(line)
        prefix = _LOG_PREFIXES[self._log_filter.currentText()]
        if prefix is None or line.startswith(prefix):
            self._log.appendPlainText(line)

    def _apply_log_filter(self) -> None:
        prefix = _LOG_PREFIXES[self._log_filter.currentText()]
        self._log.setPlainText("\n".join(
            l for l in self._log_lines if prefix is None or l.startswith(prefix)
        ))
