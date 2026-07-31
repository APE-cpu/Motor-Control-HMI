"""通信设置页面：通信方式选择、参数配置、连接控制、收发测试与日志。"""
import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

_LOG_PREFIXES = {
    "全部": None,
    "状态": "[状态]",
    "警告": "[警告]",
    "发送": "[发送]",
    "接收": "[接收]",
}

from communications.comm_manager import CommManager
from communications.protocol_v2 import (
    MessageType, ProtocolV2Error, decode_nack_payload, decode_v2_frame,
)
from communications.serial_comm import SerialComm
from config.config import (
    BAUD_RATES_CAN, BAUD_RATES_SERIAL, COMM_TYPES,
    CMD_EMERGENCY_STOP, CMD_RESET_FAULT, CMD_SET_PARAMS, CMD_SET_SENSOR,
    CMD_START, CMD_STOP, CMD_TELEMETRY, FRAME_HEADER, FRAME_TAIL,
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
    0x{CMD_TELEMETRY:02X}  v1 遥测帧（payload 格式见第三节）
    0xF0   v2 常规遥测帧（10 Hz，慢变量，载荷同 v1 串口格式）
    0xF1   v2 高速遥测帧（200 Hz UART / 1 kHz TCP 批量，电角度/Iq/相电流/Vd/Vq/Vbus）
    0xF2   v2 电流采样诊断帧（50 Hz，ADC 原始值/PWM duty/扇区/标定窗口/VDDA）

  下行（上位机 → 下位机）：
    0x{CMD_START:02X}  启动电机
    0x{CMD_STOP:02X}  停止电机（v2 会话未就绪时也允许发送，保命指令）
    0x{CMD_EMERGENCY_STOP:02X}  紧急停止（v2 会话未就绪时也允许发送，保命指令）
    0x{CMD_RESET_FAULT:02X}  故障复位（v2 专用；保护条件未清除时必须 NACK）
    0x{CMD_SET_PARAMS:02X}  下发控制参数（UTF-8 文本键值对，分号分隔）
    0x{CMD_SET_SENSOR:02X}  下发位置传感器配置（1 字节 sensor_id + 文本参数）

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
  13    uint8   flags           bit0=低速警告，bit1=过流故障，bit2=驱动故障，bit3=急停/保护锁定
  （末尾 1 字节 padding，payload 共 {TELEM_LEN} 字节）

  下位机建议发送频率 ≥10 Hz（上位机以 10 Hz 轮询显示）。

【四、CAN 总线】

  遥测上行：8 字节裸 payload（无帧头/校验/帧尾），struct "{TELEM_FMT_CAN}"：
    int16 speed_actual | uint16 speed_target | int16 current_actual(mA) | uint16 angle_raw
  上位机下行默认仲裁 ID = 0x100；旋转变压器遥测默认 ID = 0x201。
  标准帧（11 位 ID），波特率与本页「波特率」设置一致。

【五、以太网 TCP】

  STM32 作为服务端监听 5000；上位机作为客户端主动连接板卡 IP。
  TCP 直接承载 v2 帧，支持握手、命令 ACK 和遥测解析。

【六、下位机侧参考实现】

  仓库 下位机适配/ 目录提供可移植 C 协议栈：
    protocol_portable.c/h        —— 串口帧编解码（与本页协议同源）
    can_protocol_portable.c/h    —— CAN 版
    platforms/                   —— STM32 HAL / TI C2000 / Arduino / FPGA 适配示例

【七、v2 协议帧格式】

  v2 用独立的帧结构承载命令字与遥测，与 v1 区分（详见 PROTOCOL_V2.md）：

    [MAGIC 2B] [VER 1B] [ADDR 1B] [SEQ 2B] [MSG_TYPE 1B] [CMD 1B] [LEN 2B]
    [PAYLOAD × N 字节] [CRC16 2B] [TAIL 1B]

  MAGIC    = 0xA5 0x5A（与 v1 的 0xAA 区分）
  TAIL     = 0x7E
  VER      = 0x02
  CRC16    = CRC-16/CCITT-FALSE（poly=0x1021, init=0xFFFF, 不反射, xor_out=0）
             校验范围：从 VER 到 PAYLOAD 末字节
             标准校验向量："123456789" → 0x29B1
  LEN      = payload 字节数，上限 4096
  SEQ      = 16 位消息序号，把 ACK/NACK 与命令准确对应
  MSG_TYPE = 1 COMMAND / 2 ACK / 3 NACK / 4 HELLO / 5 CAPABILITIES
             6 TELEMETRY / 7 HEARTBEAT
  多字节   = 小端序

  握手：HELLO → CAPABILITIES，协商协议版本与设备身份（device_id /
        hardware_version / firmware_prefix）。不匹配立即拒绝且不降级 v1。
  心跳：周期 0.5 s、应答超时 3 s。心跳同样分配序号并要求 ACK；若高速遥测
        持续到达，则遥测作为会话存活证据，单独丢失心跳 ACK 不触发停机。
  经典 CAN 不支持 v2（v2 最小帧超过 8 字节，需先定义分片协议）。

【八、v2 三通道遥测载荷】

  v2 把遥测拆成三个独立通道，各有独立频率与字段（完整字段语义与
  PWM 比较值/采样点/扇区/VDDA 说明见《通信规约与数据字典.md》）：

  0xF0 常规遥测：载荷 15 B（串口/TCP）或 8 B（CAN 兼容），10 Hz，
                 字段与第三节 v1 串口/CAN 遥测一致。
  0xF1 高速遥测：载荷 12/16/22 B 三档，200 Hz（UART）或 1 kHz（TCP 批量），
                 含 tick_ms + angle_raw + speed_rpm + Iq + Iqref（+ Ia/Ib
                 + Vd/Vq/Vbus）。
  0xF2 电流采样诊断：载荷 21/29/31 B 三档，50 Hz，
                 含 tick_ms + ADC1/ADC2 原始码 + A/B 相零点 + SVPWM 扇区
                 + 三相 PWM 比较值 + 采样点（+ 校准窗口 + VDDA）。

  Iq/相电流标度：A = raw × 0.000629（MCSDK 默认 s16→A，对应 ADC 满量程
                 与运放增益组合，硬件变更须同步修改 comm_manager.py）。
  PWM 比较值：duty_a/b/c 是 STM32 TIM1 CCR 寄存器原始码（0..ARR），
                 不是占空比百分比；ARR 典型 5249。
  采样点：sample_point 是 ADC 注入触发计数值，对应 PWM 周期内的时刻，
                 通常设在周期中点采样纹波最小。
  扇区：sector 取 0..5，正常应循环 0→5（每 60° 电角度切换一次）；
                 恒为某值说明扇区识别或采样触发异常。

【九、v2 协议迁移状态】

  本页可选择 virtual-v2 完成纯内存联调，或选择 negotiated-v2 在真实串口/TCP
  上执行严格握手；握手失败不会降级v1。经典CAN尚未定义v2分片协议。
  v2 增加：CRC16、设备地址、16位消息序号、ACK/NACK、HELLO/CAPABILITIES 握手。
  详细格式见：下位机适配/PROTOCOL_V2.md。
  上下行数据字典与字段语义（含 PWM 比较值/采样点/扇区/VDDA）见：
  通信规约与数据字典.md。
  下位机适配完成前请勿在真实端口假定v2已生效。
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
        self.interface = QComboBox()
        self.interface.addItems(["zlgcan", "zlgcan-zcan", "slcan", "gs_usb", "socketcan", "pcan", "kvaser", "vector", "virtual"])
        # ZLG 专属参数：设备类型/设备索引/通道（仅 zlgcan / zlgcan-zcan 时显示）
        self.device_type = QSpinBox(); self.device_type.setRange(0, 99); self.device_type.setValue(4)
        self.device_index = QSpinBox(); self.device_index.setRange(0, 7); self.device_index.setValue(0)
        self.can_channel = QSpinBox(); self.can_channel.setRange(0, 7); self.can_channel.setValue(0)
        self._lbl_channel = QLabel("CAN 通道")
        self._lbl_devtype = QLabel("设备类型号")
        self._lbl_devidx = QLabel("设备索引")
        self._lbl_chn = QLabel("CAN 通道号")
        self._hint = QLabel("ZLG 设备类型号：3=USBCAN-I，4=USBCAN-II / CANalyst-II。"
                            "zlgcan=创芯 ControlCAN.dll，zlgcan-zcan=致远原厂 zlgcan.dll")
        self._hint.setWordWrap(True)
        f.addRow(self._lbl_channel, self.channel)
        f.addRow("波特率 (bps)", self.bitrate)
        f.addRow("驱动接口", self.interface)
        f.addRow(self._lbl_devtype, self.device_type)
        f.addRow(self._lbl_devidx, self.device_index)
        f.addRow(self._lbl_chn, self.can_channel)
        f.addRow("", self._hint)
        self.interface.currentTextChanged.connect(self._on_iface_changed)
        self._on_iface_changed(self.interface.currentText())

    def _on_iface_changed(self, iface: str) -> None:
        is_zlg = iface.lower() in ("zlgcan", "zlgcan-zcan")
        # ZLG 专属字段
        for w in (self.device_type, self.device_index, self.can_channel,
                  self._lbl_devtype, self._lbl_devidx, self._lbl_chn, self._hint):
            w.setVisible(is_zlg)
        # python-can 用的通道名字段
        self.channel.setVisible(not is_zlg)
        self._lbl_channel.setVisible(not is_zlg)

    def cfg(self) -> dict:
        iface = self.interface.currentText().lower()
        if iface in ("zlgcan", "zlgcan-zcan"):
            return {
                "interface": iface,
                "bitrate": int(self.bitrate.currentText()),
                "device_type": int(self.device_type.value()),
                "device_index": int(self.device_index.value()),
                "can_channel": int(self.can_channel.value()),
            }
        return {
            "channel": self.channel.text().strip(),
            "bitrate": int(self.bitrate.currentText()),
            "interface": self.interface.currentText(),
        }


class _TcpPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        f = QFormLayout(self)
        self.host = QLineEdit("192.168.1.50")
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(5000)
        self.timeout = QDoubleSpinBox(); self.timeout.setRange(1.0, 60.0); self.timeout.setValue(10.0)
        # 空 = 自动选择与板卡同网段的有线网卡，避免 WLAN/Clash TUN 抢走路由。
        self.local_host = QLineEdit("192.168.1.15")
        self.local_host.setPlaceholderText("空=自动同网段网卡")
        self.local_host.setToolTip(
            "本机源 IP。实验室有线网卡一般为 192.168.1.15。"
            "若连接失败可清空让程序自动选择，或填当前有线网卡地址。"
            "不要使用 198.18.x（代理 TUN）或 Wi-Fi 地址去连板卡。")
        f.addRow("板卡 IP", self.host)
        f.addRow("板卡端口", self.port)
        f.addRow("本机源 IP", self.local_host)
        f.addRow("连接超时 (s)", self.timeout)

    def cfg(self) -> dict:
        return {
            "host": self.host.text().strip(),
            "port": self.port.value(),
            "timeout": self.timeout.value(),
            "local_host": self.local_host.text().strip(),
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
        self._protocol_mode = QComboBox()
        self._protocol_mode.addItem("legacy-v1（兼容只读/维护，不允许启动）", "legacy-v1")
        self._protocol_mode.addItem("virtual-v2（协议联调）", "virtual-v2")
        self._protocol_mode.addItem("negotiated-v2（默认真机安全模式）", "negotiated-v2")
        self._protocol_mode.currentIndexChanged.connect(self._on_protocol_mode_changed)
        self._status_label = QLabel("未连接")
        mh.addWidget(QLabel("通信类型："))
        mh.addWidget(self._kind)
        mh.addWidget(QLabel("协议模式："))
        mh.addWidget(self._protocol_mode)
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

        # ---- 遥测档位（多级通信设置）----
        self._build_telemetry_box(root)

        # ---- 协议会话 ----
        session_box = QGroupBox("协议会话")
        sf = QFormLayout(session_box)
        self._session_state = QLabel("未激活")
        self._session_device = QLabel("—")
        self._session_version = QLabel("—")
        self._session_expected_identity = QLabel("未配置")
        self._session_expected_identity.setWordWrap(True)
        self._session_identity_verdict = QLabel("未启用白名单")
        self._session_identity_verdict.setWordWrap(True)
        self._session_pending = QLabel("0")
        self._session_result = QLabel("—")
        self._session_loss = QLabel("—")
        self._session_stats = QLabel("TX 0 / RX 0")
        self._session_stats.setWordWrap(True)
        sf.addRow("会话状态", self._session_state)
        sf.addRow("设备身份", self._session_device)
        sf.addRow("协议/固件", self._session_version)
        sf.addRow("期望身份白名单", self._session_expected_identity)
        sf.addRow("身份验证", self._session_identity_verdict)
        sf.addRow("待ACK命令", self._session_pending)
        sf.addRow("最后命令结果", self._session_result)
        sf.addRow("会话丢失原因", self._session_loss)
        sf.addRow("协议统计", self._session_stats)
        root.addWidget(session_box)

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
        lh.addWidget(self._log_filter)
        lh.addWidget(QLabel("显示："))
        self._receive_view = QComboBox()
        self._receive_view.addItems(["协议解析", "原始 HEX"])
        lh.addWidget(self._receive_view)
        lh.addStretch(1)
        lv.addLayout(lh)
        self._log = QPlainTextEdit(); self._log.setReadOnly(True)
        self._log.document().setMaximumBlockCount(2000)
        self._log_lines: list[str] = []
        lv.addWidget(self._log)
        root.addWidget(log_box, 1)

        # ---- 连接信号 ----
        comm.statusChanged.connect(self._on_status)
        comm.logMessage.connect(self._append_log)
        comm.rawReceived.connect(self._on_raw_received)
        comm.protocolSessionChanged.connect(self._on_protocol_session)

        # 初始页面 + 自动加载配置
        self._on_kind_changed(0)
        self._protocol_mode.setCurrentIndex(2)
        self._on_load_cfg(silent=True)
        self._on_protocol_mode_changed(self._protocol_mode.currentIndex())

    # ------- 遥测档位 -------
    def _build_telemetry_box(self, root) -> None:
        box = QGroupBox("遥测档位（防止遥测把心跳挤爆）")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        self._tele_level = QComboBox()
        self._tele_level.addItem("省流·安全（最稳，只留相电流）", "eco")
        self._tele_level.addItem("标准（默认：相电流 + 采样诊断）", "std")
        self._tele_level.addItem("辨识（开在线RLS，相电流降速给心跳腾队列）", "id")
        self._tele_level.addItem("自定义", "custom")
        self._tele_level.setCurrentIndex(1)   # 标准 = 固件默认
        self._tele_level.currentIndexChanged.connect(self._on_tele_changed)
        row.addWidget(QLabel("档位："))
        row.addWidget(self._tele_level, 1)
        self._tele_hint = QLabel("")
        self._tele_hint.setStyleSheet("color:#90a4ae;")
        row.addWidget(self._tele_hint)
        v.addLayout(row)

        # 高级：仅"自定义"档位展开逐通道
        self._tele_adv = QWidget()
        adv = QHBoxLayout(self._tele_adv)
        adv.setContentsMargins(0, 0, 0, 0)
        self._tele_f1 = QComboBox()
        for text, ms in (("1kHz", 1), ("500Hz", 2), ("200Hz", 5),
                         ("100Hz", 10), ("自动", 0)):
            self._tele_f1.addItem(text, ms)
        self._tele_f2_on = QCheckBox("F2诊断")
        self._tele_f2_on.setChecked(True)
        self._tele_f2 = QComboBox()
        for text, ms in (("50Hz", 20), ("20Hz", 50), ("10Hz", 100)):
            self._tele_f2.addItem(text, ms)
        self._tele_f3_on = QCheckBox("F3辨识")
        self._tele_f3 = QComboBox()
        for text, ms in (("10Hz", 100), ("5Hz", 200), ("2Hz", 500)):
            self._tele_f3.addItem(text, ms)
        adv.addWidget(QLabel("F1相电流")); adv.addWidget(self._tele_f1)
        adv.addSpacing(12)
        adv.addWidget(self._tele_f2_on); adv.addWidget(self._tele_f2)
        adv.addSpacing(12)
        adv.addWidget(self._tele_f3_on); adv.addWidget(self._tele_f3)
        adv.addStretch(1)
        for combo in (self._tele_f1, self._tele_f2, self._tele_f3):
            combo.currentIndexChanged.connect(self._on_tele_changed)
        for chk in (self._tele_f2_on, self._tele_f3_on):
            chk.toggled.connect(self._on_tele_changed)
        v.addWidget(self._tele_adv)
        self._tele_adv.setVisible(False)

        # 遥测详情展开面板
        v.addWidget(self._build_telemetry_detail())

        root.addWidget(box)
        # 固件重启会回默认，(重新)连上后自动重发当前档位保持一致
        self._comm.statusChanged.connect(self._on_tele_conn)
        self._update_tele_hint()

    def _resolve_telemetry(self):
        """把当前档位/自定义解析成 (flags, f1_ms, f2_ms, f3_ms)。"""
        key = self._tele_level.currentData()
        if key == "eco":
            return 0x00, 5, 20, 100
        if key == "std":
            return 0x01, 0, 20, 100
        if key == "id":
            return 0x03, 5, 20, 100
        flags = ((0x01 if self._tele_f2_on.isChecked() else 0) |
                 (0x02 if self._tele_f3_on.isChecked() else 0))
        return (flags, int(self._tele_f1.currentData()),
                int(self._tele_f2.currentData()), int(self._tele_f3.currentData()))

    def _update_tele_hint(self) -> None:
        flags, f1, _f2, _f3 = self._resolve_telemetry()
        if f1 == 0:
            f1txt = "自动"
        else:
            hz = round(1000 / f1)
            f1txt = "1kHz" if hz >= 1000 else f"{hz}Hz"
        self._tele_hint.setText(
            f"相电流{f1txt} · 诊断{'开' if flags & 1 else '关'} · "
            f"辨识{'开' if flags & 2 else '关'}")

    def _build_telemetry_detail(self) -> QWidget:
        """遥测档位详情面板：各通道频率、字节数、内容分解。"""
        w = QWidget()
        w.setStyleSheet("QWidget{background:#1a1f2e; border-radius:4px; padding:8px;}"
                        "QLabel{color:#b0bec5; font-size:11px;}")
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        title = QLabel("📊 遥测通道详情（实时传输内容）")
        title.setStyleSheet("font-size:12px; font-weight:bold; color:#80cbc4;")
        v.addWidget(title)

        # F0: 常规遥测 (10Hz, 慢变量)
        f0 = QLabel(
            "🔹 <b>F0 常规遥测</b> | 10 Hz | 14字节\n"
            "   转速实际值/目标值 (rpm) · Iq实际值 (mA) · 电角度 (0.01°)\n"
            "   传感器原始计数 · 温度 (°C) · 传感器质量/收敛度 · 故障标志位")
        f0.setWordWrap(True)
        v.addWidget(f0)

        # F1: 高速遥测 (200Hz~1kHz, 电流/电压)
        f1 = QLabel(
            "🔹 <b>F1 高速遥测</b> | 200 Hz (串口) / 1 kHz (TCP, 批量) | 22字节\n"
            "   电角度 (u16) · Iq (s16码值, ×0.000629→A) · 相电流 Ia/Ib (s16码值, ×0.000629→A)\n"
            "   施加电压 Vd/Vq (s16码值, ×0.000324→V) · 母线电压 Vbus (u16, 0.1V分辨率)\n"
            "   <i>16点FOC周期平均（纯遥测，控制用原始）；TCP批量发送减少帧开销</i>")
        f1.setWordWrap(True)
        v.addWidget(f1)

        # F2: ADC诊断 (20~50Hz, 采样原始)
        f2 = QLabel(
            "🔹 <b>F2 ADC诊断</b> | 20~50 Hz | 31字节 (档位可关)\n"
            "   三相ADC原始码 Ia/Ib/Ic (s16) · 三相PWM占空比寄存器 (u16)\n"
            "   扇区 (u8, 0~5循环) · 采样触发时刻 (u16) · 校准窗口百分比 (u16)\n"
            "   ADC参考电压 VDDA/VREFINT 监测 (u16×2)\n"
            "   <i>用于ADC噪声分析、采样时序验证、电源监测</i>")
        f2.setWordWrap(True)
        v.addWidget(f2)

        # F3: RLS在线辨识 (2~10Hz, 阻抗)
        f3 = QLabel(
            "🔹 <b>F3 RLS辨识</b> | 2~10 Hz | 8字节 (档位可关)\n"
            "   dq轴阻抗 Rd/Rq (float32×2, 单位Ω)\n"
            "   <i>固件16kHz RLS递推实时估计；用于参数自适应与热态监测</i>")
        f3.setWordWrap(True)
        v.addWidget(f3)

        # F4: 突发抓取 (非周期)
        f4 = QLabel(
            "🔹 <b>F4 突发抓取</b> | 非周期 (手动触发) | 2048点×6字节 分18块\n"
            "   真实16kHz原始 Ia/Ib/电角度 录128ms再慢速回传 (~0.3s)\n"
            "   <i>抓取期间暂停F1/F2/F3，每15ms发一个726字节大帧，不占心跳队列</i>")
        f4.setWordWrap(True)
        v.addWidget(f4)

        # 心跳与瓶颈说明
        note = QLabel(
            "⚠️ <b>通信瓶颈</b>：制约的是 lwIP <b>队列条目数×帧率</b>，不是带宽。\n"
            "   TCP_SND_QUEUELEN=8 (只有8槽!) + pbuf内存池 + 心跳抢占 → 高帧率小包易爆。\n"
            "   策略：F1批量(减少帧数) + F2/F3低频 + 突发慢速非实时 = 队列压力最小。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#ffab91; font-size:11px; font-style:italic;")
        v.addWidget(note)

        v.addStretch(1)
        return w

    def _send_telemetry(self) -> None:
        if not self._comm.is_connected():
            return
        flags, f1, f2, f3 = self._resolve_telemetry()
        self._comm.send_telemetry_config(flags, f1, f2, f3)

    def _on_tele_changed(self, *_args) -> None:
        self._tele_adv.setVisible(self._tele_level.currentData() == "custom")
        self._update_tele_hint()
        self._send_telemetry()

    def _on_tele_conn(self, connected: bool, *_args) -> None:
        if connected:
            self._send_telemetry()

    # ------- slots -------
    def _on_kind_changed(self, idx: int) -> None:
        # idx: 0=RS-232, 1=RS-485, 2=CAN, 3=TCP
        self._stack.setCurrentIndex(idx)

    def _on_protocol_mode_changed(self, _idx: int) -> None:
        mode = self._protocol_mode.currentData()
        virtual = mode == "virtual-v2"
        self._kind.setEnabled(not virtual)
        self._stack.setEnabled(not virtual)
        if virtual:
            self._status_label.setToolTip("virtual-v2 不打开真实端口，仅用于协议联调")
        elif mode == "negotiated-v2":
            self._status_label.setToolTip(
                "真实串口/TCP必须完成v2握手；经典CAN尚未定义分片协议")
        else:
            self._status_label.setToolTip("")

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
        if self._protocol_mode.currentData() == "virtual-v2":
            ok = self._comm.connect_virtual_v2()
            if not ok:
                QMessageBox.warning(self, "连接失败", "virtual-v2 握手失败，请查看日志。")
            return
        kind = self._kind.currentText()
        cfg = self._current_cfg()
        if self._protocol_mode.currentData() == "negotiated-v2":
            ok = self._comm.connect_negotiated_v2(kind, **cfg)
        else:
            ok = self._comm.connect(kind, **cfg)
        if not ok:
            QMessageBox.warning(
                self, "连接失败",
                "请检查参数、v2固件握手能力或查看日志；程序不会自动降级到v1。")

    def _on_disconnect(self) -> None:
        self._comm.disconnect()

    def _on_save_cfg(self) -> None:
        cfg = {"kind": self._kind.currentText(), "params": self._current_cfg(),
               "protocol_mode": self._protocol_mode.currentData()}
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
            protocol_mode = cfg.get("protocol_mode", "negotiated-v2")
            mode_idx = self._protocol_mode.findData(protocol_mode)
            if mode_idx >= 0:
                self._protocol_mode.setCurrentIndex(mode_idx)
            kind = cfg.get("kind", "")
            idx = self._kind.findText(kind)
            if idx >= 0:
                self._kind.setCurrentIndex(idx)
            params = cfg.get("params", {})
            panel = [self._serial_panel, self._serial_panel2, self._can_panel, self._tcp_panel][self._kind.currentIndex()]
            aliases = {"baudrate": "baud", "bytesize": "databits"}
            for key, val in params.items():
                w = getattr(panel, aliases.get(key, key), None)
                if w is None:
                    continue
                if hasattr(w, "setValue"):
                    w.setValue(val)
                elif hasattr(w, "setCurrentText"):
                    w.setCurrentText(str(val))
                elif hasattr(w, "setText"):
                    w.setText(str(val))
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
        if arb_id or self._receive_view.currentIndex() == 1:
            prefix = f"ID=0x{arb_id:03X}  " if arb_id else ""
            self._append_log(f"[接收] {prefix}({data.hex(' ').upper()})")
            return
        try:
            frame = decode_v2_frame(data)
        except ProtocolV2Error:
            self._append_log(f"[接收] ({data.hex(' ').upper()})")
            return

        if frame.message_type is MessageType.CAPABILITIES:
            try:
                values = json.loads(frame.payload.decode("utf-8"))
                self._append_log(
                    "[接收] 设备能力："
                    f"设备={values.get('device_id', '—')}，"
                    f"硬件={values.get('hardware_version', '—')}，"
                    f"固件={values.get('firmware_version', '—')}，"
                    f"协议=v{values.get('protocol_max', frame.version)}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._append_log("[接收] 设备能力帧（JSON无效）")
        elif frame.message_type is MessageType.TELEMETRY:
            if frame.command == 0xF1:
                # 200 Hz/1 kHz binary samples belong to the real-time plots;
                # logging every sample would flood the operator console.
                payload_length = len(frame.payload)
                valid = (payload_length == 12 or
                         (payload_length >= 16 and payload_length % 16 == 0))
                if not valid:
                    self._append_log(
                        f"[接收] 高速诊断帧长度无效：{payload_length}")
                return
            try:
                values = json.loads(frame.payload.decode("utf-8"))
                temperature_valid = not (
                    values.get("bus_state") == "uv"
                    or float(values.get("vdc", 0) or 0) < 1.0
                )
                temperature_text = (
                    f"{values.get('temperature', '—')} ℃"
                    if temperature_valid
                    else "不可用（采样电路未供电）"
                )
                self._append_log(
                    "[接收] 遥测："
                    f"转速={values.get('speed_actual', '—')} rpm，"
                    f"目标={values.get('speed_target', '—')} rpm，"
                    f"电流={values.get('current_actual', '—')} A，"
                    f"母线={values.get('vdc', '—')} V，"
                    f"温度={temperature_text}，"
                    f"温度ADC={values.get('temperature_adc', '—')}，"
                    f"故障={values.get('fault_code', '—')}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._append_log("[接收] 遥测帧（JSON无效）")
        elif frame.message_type is MessageType.ACK:
            name = "心跳" if frame.command == 0 else f"命令0x{frame.command:02X}"
            self._append_log(f"[接收] ACK：{name}，序号={frame.sequence}")
        elif frame.message_type is MessageType.NACK:
            try:
                code, message = decode_nack_payload(frame.payload)
                self._append_log(
                    f"[接收] NACK：命令0x{frame.command:02X}，"
                    f"错误={code}，原因={message}")
            except ProtocolV2Error:
                self._append_log(
                    f"[接收] NACK：命令0x{frame.command:02X}，内容无效")
        elif frame.message_type is MessageType.HELLO:
            self._append_log(f"[接收] HELLO：序号={frame.sequence}")
        elif frame.message_type is MessageType.HEARTBEAT:
            self._append_log(f"[接收] 心跳：序号={frame.sequence}")
        else:
            self._append_log(
                f"[接收] V2帧：类型={frame.message_type.name}，"
                f"序号={frame.sequence}")

    def _on_protocol_session(self, status: dict) -> None:
        state = status.get("session_state", "not_active")
        self._session_state.setText({
            "ready": "READY（握手完成）",
            "hello_sent": "HELLO_SENT",
            "incompatible": "INCOMPATIBLE",
            "not_active": "未激活",
        }.get(state, state))
        device = status.get("device_id") or "—"
        hardware = status.get("hardware_version") or ""
        self._session_device.setText(device + (f" / {hardware}" if hardware else ""))
        version = status.get("protocol_version")
        firmware = status.get("firmware_version") or "—"
        self._session_version.setText(
            f"v{version} / 固件 {firmware}" if version is not None else "—")
        expected = status.get("expected_identity", {})
        policy_active = bool(status.get("identity_policy_active"))
        if policy_active:
            parts = []
            if expected.get("device_id"):
                parts.append(f"ID={expected['device_id']}")
            if expected.get("hardware_version"):
                parts.append(f"HW={expected['hardware_version']}")
            if expected.get("firmware_prefix"):
                parts.append(f"FW前缀={expected['firmware_prefix']}")
            self._session_expected_identity.setText("　".join(parts))
            mismatch = status.get("identity_mismatch_reason") or ""
            if mismatch:
                self._session_identity_verdict.setText("失败：" + mismatch)
                self._session_identity_verdict.setStyleSheet("color: #ef5350;")
            elif status.get("identity_verified"):
                self._session_identity_verdict.setText("通过（真实v2握手身份一致）")
                self._session_identity_verdict.setStyleSheet("color: #66bb6a;")
            else:
                self._session_identity_verdict.setText("等待真实v2握手验证")
                self._session_identity_verdict.setStyleSheet("color: #ffa726;")
        else:
            self._session_expected_identity.setText("未配置")
            self._session_identity_verdict.setText(
                "未启用（仅验证v2协议兼容性，不验证具体板卡）")
            self._session_identity_verdict.setStyleSheet("color: #8fa3b8;")
        self._session_pending.setText(str(status.get("pending_ack", 0)))
        result = status.get("last_result")
        if result is None:
            self._session_result.setText("—")
        else:
            verdict = "ACK" if result.success else f"NACK/超时({result.error_code})"
            self._session_result.setText(
                f"{verdict} SEQ={result.sequence} CMD=0x{result.command:02X} "
                f"{result.message}".strip())
        self._session_loss.setText(status.get("session_lost_reason") or "—")
        stats = status.get("statistics", {})
        self._session_stats.setText(
            f"TX {stats.get('tx_frames', 0)} / RX {stats.get('rx_frames', 0)}　"
            f"遥测 {stats.get('telemetry_frames', 0)}　"
            f"ACK {stats.get('acks', 0)} / NACK {stats.get('nacks', 0)}　"
            f"超时 {stats.get('timeouts', 0)}　CRC/帧错 {stats.get('crc_or_frame_errors', 0)}　"
            f"迟到/重复ACK {stats.get('late_or_duplicate_acks', 0)}　"
            f"会话丢失 {stats.get('session_restarts_or_losses', 0)}")

    def _append_log(self, line: str) -> None:
        self._log_lines.append(line)
        if len(self._log_lines) > 5000:
            del self._log_lines[:-5000]
        prefix = _LOG_PREFIXES[self._log_filter.currentText()]
        if prefix is None or line.startswith(prefix):
            self._log.appendPlainText(line)

    def _apply_log_filter(self) -> None:
        prefix = _LOG_PREFIXES[self._log_filter.currentText()]
        self._log.setPlainText("\n".join(
            l for l in self._log_lines if prefix is None or l.startswith(prefix)
        ))
