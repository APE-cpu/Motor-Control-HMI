"""通信管理器：上层统一入口。

- 维护当前的通信驱动（串口或 CAN）
- 提供连接 / 断开 / 发送 / 接收
- 在后台线程上轮询数据，通过 Qt 信号通知上层
- 当未连接时，提供模拟数据源，便于在没有真实下位机时进行 UI 演示
"""
import json
import math
import random
import struct
import threading
import time
from typing import Optional, Tuple

from PySide6.QtCore import QObject, Signal

from config.config import (
    CMD_EMERGENCY_STOP, CMD_START, CMD_STOP, CMD_TELEMETRY,
    FRAME_HEADER, FRAME_TAIL,
    TELEM_ANGLE_SCALE, TELEM_CURRENT_SCALE, TELEM_FMT, TELEM_FMT_CAN,
    TELEM_LEN, TELEM_LEN_CAN, TELEM_TEMP_OFFSET, TELEM_TORQUE_FROM_CURRENT,
    TELEM_FLAG_DRIVER_FAULT, TELEM_FLAG_EMERGENCY_FAULT,
    TELEM_FLAG_LOW_SPEED_WARN, TELEM_FLAG_OVERCURRENT_FAULT,
)

from .base_comm import BaseComm
from .can_comm import CANComm
from .motor_sim import MotorSim
from .protocol import decode_frame
from .serial_comm import SerialComm
from .tcp_comm import TCPComm
from .zlgcan_comm import ZlgCanComm
from .zlgcan_zcan_comm import ZlgCanZcanComm
from .protocol_session import (
    CommandResult, ProtocolSession, ProtocolSessionState,
)
from .protocol_v2 import (
    MessageType, ProtocolV2Error, V2Frame, V2StreamDecoder,
    decode_v2_frame, encode_v2_frame,
)
from .v2_virtual_device import V2VirtualDevice


class TelemetryFrame:
    """一帧遥测数据（监控页面消费）。"""

    __slots__ = (
        "speed_actual", "speed_target",
        "current_actual", "current_target",
        "torque_actual", "torque_target",
        "angle_actual", "temperature",
        # 母线/硬件侧字段
        "vdc",             # 直流母线电压 V
        "bus_state",       # "normal" / "brake"(斩波) / "uv"(欠压) / "ov"(过压跳闸)
        "powers",          # 功率流快照 dict（W），键见 MotorSim；真机暂为 {}
        # 传感器差异化字段
        "sensor_source",   # 当前活跃传感器名称
        "sensor_quality",  # 0-1 之间的数据质量/置信度
        "angle_raw",       # 传感器原始量（Hall=离散步、QEP=脉冲计数、其它=估算值）
        "convergence",     # 无传感器观测器收敛度 0-1；有传感器时恒为 1.0
        "low_speed_warn",  # 低速段是否进入不可用区
        "fault_code",      # 下位机故障位掩码；0 表示无锁定故障
        "fault_text",      # 可读故障原因
        "mc_state",        # MCSDK状态机原始值；0=IDLE, 6=RUN, 11=FAULT_OVER
        "max_rpm",         # 下位机实际采用的最高转速限制
        "current_limit_a", # 下位机实际采用的Iq限流
        "runaway_limit_rpm", # 当前动态跑飞阈值
        "phase_current_a", # 相电流幅值，区别于current_actual(q轴电流)
        "speed_kp",        # 下位机速度环实际Kp数字量
        "speed_ki",        # 下位机速度环实际Ki数字量
        "data_source",     # "sim" / "real" / "real_partial"
    )

    def __init__(self) -> None:
        self.speed_actual = 0.0
        self.speed_target = 0.0
        self.current_actual = 0.0
        self.current_target = 0.0
        self.torque_actual = 0.0
        self.torque_target = 0.0
        self.angle_actual = 0.0
        self.temperature = 25.0
        self.vdc = 48.0
        self.bus_state = "normal"
        self.powers = {}
        self.sensor_source = ""
        self.sensor_quality = 1.0
        self.angle_raw = 0.0
        self.convergence = 1.0
        self.low_speed_warn = False
        self.fault_code = 0
        self.fault_text = ""
        self.mc_state = 0
        self.max_rpm = 0.0
        self.current_limit_a = 0.0
        self.runaway_limit_rpm = 0.0
        self.phase_current_a = 0.0
        self.speed_kp = 0
        self.speed_ki = 0
        self.data_source = "sim"


class CommManager(QObject):
    """通信管理器（线程安全），通过 Qt 信号将数据推给 UI。"""

    statusChanged = Signal(bool, str)        # 连接状态 + 备注
    telemetryReceived = Signal(object)        # TelemetryFrame
    highRateTelemetryReceived = Signal(object) # 200Hz紧凑诊断样本dict
    currentSamplingDiagReceived = Signal(object) # F2 ADC/PWM采样诊断
    logMessage = Signal(str)                  # 日志/错误信息
    rawReceived = Signal(int, bytes)          # CAN原始帧 (arbitration_id, data)
    faultDetected = Signal(str)               # 需要进入 FAULT_LOCKED 的故障
    protocolSessionChanged = Signal(object)   # 会话状态快照 dict
    commandResult = Signal(object)            # v2 CommandResult

    def __init__(self) -> None:
        super().__init__()
        self._driver: Optional[BaseComm] = None
        self._kind: str = ""
        self._cfg: dict = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sim_t = 0.0
        # 当前活跃位置传感器（影响仿真分支与 CAN ID 透传），默认 QEP
        self._active_sensor_id: int = 1
        self._active_sensor_name: str = "增量式编码器(QEP)"
        self._rx_buf = bytearray()
        self._latest_frame = TelemetryFrame()
        self._consecutive_read_errors = 0
        self._poll_started_at = 0.0
        self._last_valid_at = 0.0
        self._reported_faults: set[str] = set()
        self._protocol_mode = "legacy-v1"
        self._v2_session: ProtocolSession | None = None
        self._v2_device: V2VirtualDevice | None = None
        self._v2_now = 0.0
        self._v2_lock = threading.RLock()
        self._last_command_result: CommandResult | None = None
        self._v2_pending_legacy: dict[int, bytes] = {}
        self._v2_last_heartbeat_at = 0.0
        self._v2_connection_lost_reported = False
        self._v2_stream_decoder: V2StreamDecoder | None = None
        self._protocol_stats = self._new_protocol_stats()
        self._expected_device_identity = {
            "device_id": "", "hardware_version": "", "firmware_prefix": "",
        }
        # 数字孪生 L1：PMSM 物理模型（虚拟下位机）
        self._motor_sim = MotorSim()

    # ------------------ 公共接口 ------------------
    def connect(self, kind: str, **cfg) -> bool:
        """kind: "RS-232" / "RS-485" / "CAN总线"。"""
        self.disconnect()
        try:
            self._driver = self._open_driver(kind, cfg)
            self._kind = kind
            self._cfg = cfg
            self._protocol_mode = "legacy-v1"
            self._start_poll()
            self.statusChanged.emit(True, f"{kind} 已连接")
            self.logMessage.emit(f"[通信] {kind} 已连接，参数={cfg}")
            return True
        except Exception as exc:
            self.logMessage.emit(f"[错误] 连接失败：{exc}")
            self.statusChanged.emit(False, str(exc))
            self._driver = None
            return False

    def connect_negotiated_v2(
            self, kind: str, *, address: int = 1,
            handshake_timeout_s: float = 1.5,
            driver: BaseComm | None = None, **cfg) -> bool:
        """在真实字节流驱动上建立v2会话；握手失败绝不降级到v1。

        ``driver`` 仅用于驱动级测试或外部适配器注入。经典CAN在定义分片协议前
        不允许承载v2，以免超过8字节的帧被底层静默截断。
        """
        self.disconnect()
        self._protocol_mode = "negotiated-v2"
        self._kind = kind
        self._cfg = dict(cfg)
        self._protocol_stats = self._new_protocol_stats()
        session = ProtocolSession(address=address)
        self._v2_session = session
        self._v2_stream_decoder = V2StreamDecoder()
        try:
            if kind == "CAN总线":
                raise ProtocolV2Error(
                    "经典CAN暂不支持v2：必须先定义8字节分片与重组协议")
            self._driver = driver or self._open_driver(kind, cfg)
            if driver is not None and not driver.is_open():
                driver.open(**cfg)

            hello = encode_v2_frame(session.build_hello())
            self._send_real_v2_wire(hello)
            deadline = time.monotonic() + max(0.05, float(handshake_timeout_s))
            capabilities = None
            while time.monotonic() < deadline:
                chunk = self._driver.recv(size=512, timeout=0.05)
                if not chunk:
                    continue
                frames = self._decode_real_v2_chunk(chunk)
                for frame in frames:
                    self._protocol_stats["rx_frames"] += 1
                    self.rawReceived.emit(0, encode_v2_frame(frame))
                    if frame.message_type is not MessageType.CAPABILITIES:
                        continue
                    capabilities = session.handle_frame(frame)
                    break
                if capabilities is not None:
                    break
            if session.state is not ProtocolSessionState.READY:
                raise ProtocolV2Error(
                    session.incompatible_reason or "v2握手超时或未收到CAPABILITIES")
            identity_error = self._identity_mismatch_reason(capabilities)
            if identity_error:
                raise ProtocolV2Error(identity_error)

            self._v2_last_heartbeat_at = time.monotonic()
            self._v2_connection_lost_reported = False
            self._protocol_stats["handshakes"] += 1
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._real_v2_poll_loop, daemon=True)
            self._thread.start()
            self._emit_protocol_snapshot()
            self.statusChanged.emit(
                True, f"{kind} v2已握手：{capabilities.device_id}")
            self.logMessage.emit(
                f"[状态] 真实v2握手成功 设备={capabilities.device_id} "
                f"固件={capabilities.firmware_version} 协议={session.negotiated_version}")
            return True
        except Exception as exc:
            if session.state is not ProtocolSessionState.INCOMPATIBLE:
                session.invalidate(str(exc))
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception:
                    pass
            self._driver = None
            self.logMessage.emit(f"[错误] negotiated-v2连接失败：{exc}；未降级到v1")
            self.statusChanged.emit(False, str(exc))
            self._emit_protocol_snapshot()
            return False

    def connect_virtual_v2(self, device: V2VirtualDevice | None = None) -> bool:
        """连接纯内存v2虚拟下位机；不会打开任何真实端口。"""
        self.disconnect()
        try:
            self._protocol_stats = self._new_protocol_stats()
            session = ProtocolSession(address=(device.address if device else 1))
            virtual = device or V2VirtualDevice(address=session.address)
            hello = encode_v2_frame(session.build_hello())
            self._protocol_stats["tx_frames"] += 1
            responses = virtual.receive_bytes(hello, now_s=0.0)
            if len(responses) != 1:
                raise ProtocolV2Error("虚拟设备握手无响应")
            self._protocol_stats["rx_frames"] += 1
            capabilities = session.handle_frame(decode_v2_frame(responses[0]))
            if session.state is not ProtocolSessionState.READY:
                raise ProtocolV2Error(session.incompatible_reason or "v2握手失败")
            self._protocol_mode = "virtual-v2"
            self._kind = "virtual-v2"
            self._cfg = {"device_id": capabilities.device_id}
            self._v2_session = session
            self._v2_device = virtual
            self._v2_now = 0.0
            self._v2_last_heartbeat_at = 0.0
            self._v2_connection_lost_reported = False
            self._protocol_stats["handshakes"] += 1
            self._motor_sim.reset()
            self._stop.clear()
            self._thread = threading.Thread(target=self._v2_poll_loop, daemon=True)
            self._thread.start()
            self._emit_protocol_snapshot()
            self.statusChanged.emit(True, f"virtual-v2 已握手：{capabilities.device_id}")
            self.logMessage.emit(
                f"[状态] v2握手成功 设备={capabilities.device_id} "
                f"固件={capabilities.firmware_version} 协议={session.negotiated_version}")
            return True
        except Exception as exc:
            self._v2_session = None
            self._v2_device = None
            self._protocol_mode = "legacy-v1"
            self.logMessage.emit(f"[错误] virtual-v2 连接失败：{exc}")
            self.statusChanged.emit(False, str(exc))
            self._emit_protocol_snapshot()
            return False

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception as exc:
                self.logMessage.emit(f"[错误] 关闭失败：{exc}")
        self._driver = None
        self._v2_session = None
        self._v2_device = None
        self._v2_stream_decoder = None
        self._protocol_mode = "legacy-v1"
        self._last_command_result = None
        self._v2_pending_legacy.clear()
        self._rx_buf.clear()
        self._emit_protocol_snapshot()
        self.statusChanged.emit(False, "已断开")

    def is_connected(self) -> bool:
        if (self._protocol_mode in ("virtual-v2", "negotiated-v2") and
                self._v2_session is not None):
            return self._v2_session.state is ProtocolSessionState.READY
        return self._driver is not None and self._driver.is_open()

    def set_active_sensor(self, sensor_id: int, sensor_name: str = "") -> None:
        self._active_sensor_id = int(sensor_id)
        if sensor_name:
            self._active_sensor_name = sensor_name

    def is_sim_running(self) -> bool:
        """仿真数据流是否在跑（未连接真机、后台线程存活）。"""
        if self._protocol_mode == "virtual-v2":
            return self._thread is not None and self._thread.is_alive()
        return (not self.is_connected()
                and self._thread is not None and self._thread.is_alive())

    def protocol_status(self) -> dict:
        session = self._v2_session
        caps = session.capabilities if session else None
        policy_active = any(self._expected_device_identity.values())
        identity_error = self._identity_mismatch_reason(caps) if caps else ""
        return {
            "mode": self._protocol_mode,
            "session_state": session.state.value if session else "not_active",
            "device_id": caps.device_id if caps else "",
            "firmware_version": caps.firmware_version if caps else "",
            "hardware_version": caps.hardware_version if caps else "",
            "protocol_version": session.negotiated_version if session else None,
            "pending_ack": len(session.pending_sequences) if session else 0,
            "last_result": self._last_command_result,
            "session_lost_reason": session.session_lost_reason if session else "",
            "statistics": dict(self._protocol_stats),
            "expected_identity": dict(self._expected_device_identity),
            "identity_policy_active": policy_active,
            "identity_verified": (
                self._protocol_mode == "negotiated-v2" and caps is not None and
                policy_active and not identity_error),
            "identity_mismatch_reason": identity_error,
        }

    def configure_expected_device_identity(
            self, device_id: str = "", hardware_version: str = "",
            firmware_prefix: str = "") -> bool:
        """配置真实v2身份白名单；空字段表示该维度不限制。"""
        self._expected_device_identity = {
            "device_id": str(device_id).strip(),
            "hardware_version": str(hardware_version).strip(),
            "firmware_prefix": str(firmware_prefix).strip(),
        }
        session = self._v2_session
        mismatch = ""
        if (self._protocol_mode == "negotiated-v2" and session is not None and
                session.capabilities is not None):
            mismatch = self._identity_mismatch_reason(session.capabilities)
            if mismatch and session.state is ProtocolSessionState.READY:
                self._handle_v2_session_loss(mismatch)
        self._emit_protocol_snapshot()
        return not mismatch

    def virtual_v2_device(self) -> V2VirtualDevice | None:
        """测试/故障注入入口；真实协议模式下返回None。"""
        return self._v2_device

    def motor_sim_params(self):
        """数字孪生的 PMSM 参数对象（参数辨识页可读写）。"""
        return self._motor_sim.p

    def configure_motor_current_limit(self, current_a: float) -> None:
        """设置数字孪生电流给定限幅；真机值同时由控制页参数帧下发。"""
        value = float(current_a)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError("电流限幅必须为大于0的有限数值")
        self._motor_sim.p.i_max = value

    def latest_frame(self) -> TelemetryFrame:
        """返回最近遥测对象；调用方只读使用。"""
        return self._latest_frame

    def set_sim_load(self, torque_nm: float) -> None:
        """给虚拟电机注入外部负载转矩（扫频/测功机加载，仅仿真有效）。"""
        self._motor_sim.set_load(torque_nm)

    def pulse_sim_load(self, delta_nm: float, duration_s: float = 1.0) -> None:
        """一次性负载阶跃扰动（突加/突卸测试，仅仿真有效）。"""
        self._motor_sim.pulse_load(delta_nm, duration_s)

    def set_sim_load_disturbance(self, amplitude_nm: float,
                                 period_s: float = 2.0) -> None:
        """周期方波负载扰动（持续起伏，仅仿真有效）。amplitude=0 关闭。"""
        self._motor_sim.set_load_disturbance(amplitude_nm, period_s)

    def motor_sim_trace(self) -> list:
        """取走虚拟电机的高速轨迹缓冲 [(θe, id, iq), ...]（1 kHz）。"""
        pts = list(self._motor_sim.trace)
        self._motor_sim.trace.clear()
        return pts

    def _dispatch_to_sim(self, data: bytes) -> bool:
        """仿真模式下把控制帧交给虚拟电机执行（数字孪生 L1）。"""
        decoded = decode_frame(data)
        if decoded is None:
            self.logMessage.emit("[仿真] 非法帧，虚拟电机忽略")
            return False
        cmd, payload = decoded
        if cmd == CMD_START:
            target = None
            try:
                text = payload.decode("utf-8")
                for part in text.split(";"):
                    if part.startswith("target="):
                        target = float(part.split("=", 1)[1])
            except Exception:
                pass
            self._motor_sim.start(target)
            self.logMessage.emit(
                f"[仿真] 虚拟电机启动，目标转速 {self._motor_sim.speed_ref_rpm:.0f} rpm")
        elif cmd == CMD_STOP:
            self._motor_sim.stop()
            self.logMessage.emit("[仿真] 虚拟电机停止（滑行）")
        elif cmd == CMD_EMERGENCY_STOP:
            self._motor_sim.emergency_stop()
            self.logMessage.emit("[仿真] 虚拟电机紧急停止")
        else:
            self.logMessage.emit(f"[仿真] 虚拟电机收到 CMD=0x{cmd:02X}（未实现，忽略）")
        return True

    def send_frame(self, data: bytes) -> bool:
        if self._protocol_mode == "virtual-v2":
            return self._send_virtual_v2(data)
        if self._protocol_mode == "negotiated-v2":
            return self._send_negotiated_v2(data)
        if not self.is_connected():
            if self.is_sim_running():
                return self._dispatch_to_sim(data)
            self.logMessage.emit("[警告] 当前未连接，无法发送")
            return False
        try:
            self._driver.send(data)
            self.logMessage.emit(f"[发送] {data.hex(' ')}")
            return True
        except Exception as exc:
            self.logMessage.emit(f"[错误] 发送失败：{exc}")
            return False

    def send_frame_with_id(self, data: bytes, can_id: int = 0x100) -> bool:
        if self._protocol_mode == "virtual-v2":
            return self._send_virtual_v2(data)
        if self._protocol_mode == "negotiated-v2":
            return self._send_negotiated_v2(data)
        if not self.is_connected():
            if self.is_sim_running():
                return self._dispatch_to_sim(data)
            self.logMessage.emit("[警告] 当前未连接，无法发送")
            return False
        try:
            if self._kind == "CAN总线":
                self._driver.send(data, arbitration_id=can_id)
                self.logMessage.emit(f"[发送] CAN ID=0x{can_id:03X} {data.hex(' ')}")
            else:
                self._driver.send(data)
                self.logMessage.emit(f"[发送] {data.hex(' ')}")
            return True
        except Exception as exc:
            self.logMessage.emit(f"[错误] 发送失败：{exc}")
            return False

    def _send_virtual_v2(self, legacy_frame: bytes) -> bool:
        decoded = decode_frame(legacy_frame)
        if decoded is None:
            self.logMessage.emit("[错误] v2迁移层收到非法v1语义帧")
            return False
        command, payload = decoded
        with self._v2_lock:
            if self._v2_session is None or self._v2_device is None:
                return False
            try:
                request = self._v2_session.build_command(command, payload)
                self._v2_pending_legacy[request.sequence] = legacy_frame
                wire = encode_v2_frame(request)
                self._protocol_stats["tx_frames"] += 1
                self.logMessage.emit(
                    f"[发送] v2 SEQ={request.sequence} CMD=0x{command:02X} {wire.hex(' ')}")
                responses = self._v2_device.receive_bytes(wire, self._v2_now)
                results = self._process_v2_responses(responses)
            except Exception as exc:
                self.logMessage.emit(f"[错误] v2命令发送失败：{exc}")
                return False
        result = next((item for item in results if isinstance(item, CommandResult)), None)
        if result is None:
            self._emit_protocol_snapshot()
            self.logMessage.emit(f"[警告] v2命令等待ACK SEQ={request.sequence}")
            return False
        if result.success:
            return True
        self.logMessage.emit(
            f"[错误] v2 NACK CMD=0x{command:02X} CODE={result.error_code} {result.message}")
        return False

    def _send_negotiated_v2(self, legacy_frame: bytes) -> bool:
        """把现有页面的v1语义命令封装为真实v2 COMMAND。

        返回False表示尚未得到ACK，而不是回退或发送v1；最终结果由
        commandResult异步推进运行状态机。
        """
        decoded = decode_frame(legacy_frame)
        if decoded is None:
            self.logMessage.emit("[错误] v2迁移层收到非法v1语义帧")
            return False
        command, payload = decoded
        with self._v2_lock:
            if self._driver is None or not self._driver.is_open():
                self.logMessage.emit("[警告] negotiated-v2会话未就绪，命令被拒绝")
                return False
            if (self._v2_session is None or
                    self._v2_session.state is not ProtocolSessionState.READY):
                if command in (CMD_STOP, CMD_EMERGENCY_STOP):
                    try:
                        wire = encode_v2_frame(V2Frame(
                            MessageType.COMMAND, command=command,
                            payload=payload, address=1, sequence=0xFFFF))
                        self._send_real_v2_wire(wire)
                        self.logMessage.emit(
                            f"[发送] 无会话安全命令 CMD=0x{command:02X}")
                        return True
                    except Exception as exc:
                        self.logMessage.emit(f"[错误] 无会话安全命令发送失败：{exc}")
                        return False
                self.logMessage.emit("[警告] negotiated-v2会话未就绪，命令被拒绝")
                return False
            try:
                request = self._v2_session.build_command(command, payload)
            except ProtocolV2Error as exc:
                # Capability rejection is local command validation, not a
                # broken transport/session.  Keep heartbeat and safety
                # commands available.
                self.logMessage.emit(
                    f"[错误] v2命令未发送 CMD=0x{command:02X}：{exc}")
                return False
            try:
                self._v2_pending_legacy[request.sequence] = legacy_frame
                wire = encode_v2_frame(request)
                self._send_real_v2_wire(wire)
                self.logMessage.emit(
                    f"[发送] 真实v2 SEQ={request.sequence} "
                    f"CMD=0x{command:02X} {wire.hex(' ')}")
            except Exception as exc:
                self._handle_v2_session_loss(f"v2命令发送失败：{exc}")
                return False
        self._emit_protocol_snapshot()
        self.logMessage.emit(f"[状态] 真实v2命令等待ACK SEQ={request.sequence}")
        return False

    def _process_v2_responses(self, responses: list[bytes]) -> list[object]:
        outputs: list[object] = []
        for raw in responses:
            self._protocol_stats["rx_frames"] += 1
            self.rawReceived.emit(0, raw)
            try:
                frame = decode_v2_frame(raw)
            except ProtocolV2Error as exc:
                self._protocol_stats["crc_or_frame_errors"] += 1
                self.logMessage.emit(f"[错误] v2响应无效：{exc}")
                continue
            if frame.message_type is MessageType.TELEMETRY:
                self._protocol_stats["telemetry_frames"] += 1
                if frame.command == 0xF1:
                    if len(frame.payload) not in (12, 16):
                        self._protocol_stats["crc_or_frame_errors"] += 1
                        continue
                    tick_ms, angle_raw, speed_rpm, iq_raw, iqref_raw = \
                        struct.unpack("<IHhhh", frame.payload[:12])
                    sample = {
                        "tick_ms": tick_ms,
                        "angle_deg": angle_raw * 360.0 / 65536.0,
                        "speed_rpm": float(speed_rpm),
                        "iq_a": iq_raw * 0.000629,
                        "iqref_a": iqref_raw * 0.000629,
                    }
                    if len(frame.payload) == 16:
                        ia_raw, ib_raw = struct.unpack("<hh", frame.payload[12:])
                        sample["ia_a"] = ia_raw * 0.000629
                        sample["ib_a"] = ib_raw * 0.000629
                    self.highRateTelemetryReceived.emit(sample)
                    outputs.append(sample)
                    continue
                if frame.command == 0xF2:
                    if len(frame.payload) not in (21, 29):
                        self._protocol_stats["crc_or_frame_errors"] += 1
                        continue
                    values = struct.unpack("<IHHHHBHHHH", frame.payload[:21])
                    sample = {
                        "tick_ms": values[0], "adc1_raw": values[1],
                        "adc2_raw": values[2], "offset_a": values[3],
                        "offset_b": values[4], "sector": values[5],
                        "duty_a": values[6], "duty_b": values[7],
                        "duty_c": values[8], "sample_point": values[9],
                    }
                    # STM32F4 injected left-aligned data uses bit 15 as sign;
                    # the unsigned 12-bit result therefore spans 0..32760.
                    # MCSDK doubles it before offset subtraction.
                    adc_volts_per_count = 3.3 / 32768.0
                    amps_per_mc_digit = 3.3 / (65536.0 * 0.01 * 8.0)
                    sample.update({
                        "adc1_v": values[1] * adc_volts_per_count,
                        "adc2_v": values[2] * adc_volts_per_count,
                        "zero_a_v": (values[3] / 2.0) * adc_volts_per_count,
                        "zero_b_v": (values[4] / 2.0) * adc_volts_per_count,
                        "adc1_delta_a": (values[3] - 2 * values[1]) * amps_per_mc_digit,
                        "adc2_delta_a": (values[4] - 2 * values[2]) * amps_per_mc_digit,
                    })
                    if len(frame.payload) == 29:
                        cal = struct.unpack("<HHHH", frame.payload[21:29])
                        sample.update({
                            "cal_adc1_min": cal[0], "cal_adc1_max": cal[1],
                            "cal_adc2_min": cal[2], "cal_adc2_max": cal[3],
                            "cal_adc1_pp": cal[1] - cal[0],
                            "cal_adc2_pp": cal[3] - cal[2],
                        })
                    self.currentSamplingDiagReceived.emit(sample)
                    outputs.append(sample)
                    continue
                try:
                    telemetry = self._parse_v2_telemetry(frame)
                except ProtocolV2Error as exc:
                    self._protocol_stats["crc_or_frame_errors"] += 1
                    self.logMessage.emit(f"[错误] v2遥测无效：{exc}")
                    continue
                self._latest_frame = telemetry
                self._inspect_frame_fault(telemetry)
                self.telemetryReceived.emit(telemetry)
                outputs.append(telemetry)
                continue
            if self._v2_session is None:
                continue
            try:
                result = self._v2_session.handle_frame(frame)
            except ProtocolV2Error as exc:
                self._protocol_stats["crc_or_frame_errors"] += 1
                self.logMessage.emit(f"[错误] v2会话帧无效：{exc}")
                continue
            if isinstance(result, CommandResult):
                if result.success:
                    self._protocol_stats["acks"] += 1
                else:
                    self._protocol_stats["nacks"] += 1
                if result.command == 0:  # HEARTBEAT
                    if not result.success:
                        self._handle_v2_session_loss(
                            result.message or "设备心跳拒绝，可能已经重启")
                    outputs.append(result)
                    continue
                self._last_command_result = result
                legacy_frame = self._v2_pending_legacy.pop(result.sequence, None)
                if (result.success and legacy_frame is not None and
                        self._protocol_mode == "virtual-v2"):
                    self._dispatch_to_sim(legacy_frame)
                self.commandResult.emit(result)
                self.logMessage.emit(
                    f"[状态] v2 {'ACK' if result.success else 'NACK'} "
                    f"SEQ={result.sequence} CMD=0x{result.command:02X}")
                outputs.append(result)
            elif frame.message_type in (MessageType.ACK, MessageType.NACK):
                self._protocol_stats["late_or_duplicate_acks"] += 1
        self._emit_protocol_snapshot()
        return outputs

    def _parse_v2_telemetry(self, frame: V2Frame) -> TelemetryFrame:
        try:
            values = json.loads(frame.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolV2Error("v2遥测payload无效") from exc
        telemetry = TelemetryFrame()
        for field in TelemetryFrame.__slots__:
            if field in values:
                setattr(telemetry, field, values[field])
        telemetry.data_source = (
            "sim" if self._protocol_mode == "virtual-v2" else "real")
        return telemetry

    def _v2_poll_loop(self) -> None:
        while not self._stop.is_set():
            with self._v2_lock:
                self._v2_now += 0.1
                if self._v2_device is None:
                    break
                sim_frame = self._make_simulated_frame()
                values = {
                    field: getattr(sim_frame, field)
                    for field in TelemetryFrame.__slots__ if field != "powers"
                }
                responses = self._v2_device.emit_telemetry(values, self._v2_now)
                if (self._v2_session is not None and
                        self._v2_session.state is ProtocolSessionState.READY and
                        self._v2_now - self._v2_last_heartbeat_at >= 0.5 and
                        not self._v2_session.has_pending_command(0)):
                    heartbeat = self._v2_session.build_heartbeat()
                    self._protocol_stats["tx_frames"] += 1
                    responses += self._v2_device.receive_bytes(
                        encode_v2_frame(heartbeat), self._v2_now)
                    self._v2_last_heartbeat_at = self._v2_now
                responses += self._v2_device.poll(self._v2_now)
                self._process_v2_responses(responses)
                if self._v2_session is not None:
                    expired = self._v2_session.expire_commands(1.0)
                    for result in expired:
                        self._v2_pending_legacy.pop(result.sequence, None)
                        self._protocol_stats["timeouts"] += 1
                        if result.command == 0:
                            self._handle_v2_session_loss("设备心跳ACK超时，会话已失效")
                            continue
                        self._last_command_result = result
                        self.commandResult.emit(result)
                        self.logMessage.emit(
                            f"[错误] v2 ACK超时 SEQ={result.sequence} "
                            f"CMD=0x{result.command:02X}")
                    if expired:
                        self._emit_protocol_snapshot()
            time.sleep(0.1)

    def _emit_protocol_snapshot(self) -> None:
        self.protocolSessionChanged.emit(self.protocol_status())

    def _handle_v2_session_loss(self, reason: str) -> None:
        if self._v2_connection_lost_reported or self._v2_session is None:
            return
        self._v2_connection_lost_reported = True
        self._protocol_stats["session_restarts_or_losses"] += 1
        invalidated = self._v2_session.invalidate(reason)
        for result in invalidated:
            self._v2_pending_legacy.pop(result.sequence, None)
            if result.command != 0:
                self.commandResult.emit(result)
        self.logMessage.emit(f"[错误] v2会话失效：{reason}，必须重新握手")
        self.statusChanged.emit(False, f"v2会话失效：{reason}")
        self._emit_protocol_snapshot()

    @staticmethod
    def _new_protocol_stats() -> dict:
        return {
            "tx_frames": 0,
            "rx_frames": 0,
            "telemetry_frames": 0,
            "crc_or_frame_errors": 0,
            "acks": 0,
            "nacks": 0,
            "timeouts": 0,
            "late_or_duplicate_acks": 0,
            "handshakes": 0,
            "session_restarts_or_losses": 0,
        }

    def _identity_mismatch_reason(self, capabilities) -> str:
        if capabilities is None:
            return ""
        expected = self._expected_device_identity
        if (expected["device_id"] and
                capabilities.device_id != expected["device_id"]):
            return (f"设备身份不匹配：期望{expected['device_id']}，"
                    f"实际{capabilities.device_id}")
        if (expected["hardware_version"] and
                capabilities.hardware_version != expected["hardware_version"]):
            return (f"硬件版本不匹配：期望{expected['hardware_version']}，"
                    f"实际{capabilities.hardware_version}")
        if (expected["firmware_prefix"] and
                not capabilities.firmware_version.startswith(
                    expected["firmware_prefix"])):
            return (f"固件版本不匹配：期望前缀{expected['firmware_prefix']}，"
                    f"实际{capabilities.firmware_version}")
        return ""

    @staticmethod
    def _open_driver(kind: str, cfg: dict) -> BaseComm:
        if kind in ("RS-232", "RS-485"):
            driver: BaseComm = SerialComm()
        elif kind == "CAN总线":
            iface = str(cfg.get("interface", "")).lower()
            if iface == "zlgcan":
                driver = ZlgCanComm()
            elif iface == "zlgcan-zcan":
                driver = ZlgCanZcanComm()
            else:
                driver = CANComm()
        elif kind == "以太网TCP":
            driver = TCPComm()
        else:
            raise ValueError(f"未知通信方式: {kind}")
        driver.open(**cfg)
        return driver

    def _send_real_v2_wire(self, wire: bytes) -> None:
        if self._driver is None or not self._driver.is_open():
            raise RuntimeError("真实通信驱动未打开")
        sent = int(self._driver.send(wire))
        if sent != len(wire):
            raise RuntimeError(f"v2帧未完整发送：{sent}/{len(wire)}字节")
        self._protocol_stats["tx_frames"] += 1

    def _decode_real_v2_chunk(self, chunk: bytes) -> list[V2Frame]:
        if self._v2_stream_decoder is None:
            return []
        before = self._v2_stream_decoder.error_count
        frames = self._v2_stream_decoder.feed(chunk)
        errors = self._v2_stream_decoder.error_count - before
        if errors:
            self._protocol_stats["crc_or_frame_errors"] += errors
            self.logMessage.emit(f"[错误] 真实v2字节流丢弃{errors}个损坏帧")
        return frames

    def _real_v2_poll_loop(self) -> None:
        """真实串口/TCP v2轮询；所有状态推进只依据有效帧和ACK。"""
        while not self._stop.is_set():
            with self._v2_lock:
                if self._driver is None or not self._driver.is_open():
                    self._handle_v2_session_loss("真实通信链路已关闭")
                    break
                try:
                    chunk = self._driver.recv(size=512, timeout=0.05)
                except Exception as exc:
                    self._handle_v2_session_loss(f"真实v2读取失败：{exc}")
                    break
                if chunk:
                    frames = self._decode_real_v2_chunk(chunk)
                    if frames:
                        self._process_v2_responses(
                            [encode_v2_frame(frame) for frame in frames])

                now = time.monotonic()
                session = self._v2_session
                if (session is not None and
                        session.state is ProtocolSessionState.READY and
                        now - self._v2_last_heartbeat_at >= 0.5 and
                        not session.has_pending_command(0)):
                    try:
                        self._send_real_v2_wire(
                            encode_v2_frame(session.build_heartbeat()))
                        self._v2_last_heartbeat_at = now
                    except Exception as exc:
                        self._handle_v2_session_loss(f"心跳发送失败：{exc}")
                        break

                if session is not None:
                    expired = session.expire_commands(
                        1.0, now=now, heartbeat_timeout_s=3.0)
                    for result in expired:
                        self._v2_pending_legacy.pop(result.sequence, None)
                        self._protocol_stats["timeouts"] += 1
                        if result.command == 0:
                            self._handle_v2_session_loss(
                                "设备心跳ACK超时，会话已失效")
                            continue
                        self._last_command_result = result
                        self.commandResult.emit(result)
                        self.logMessage.emit(
                            f"[错误] 真实v2 ACK超时 SEQ={result.sequence} "
                            f"CMD=0x{result.command:02X}")
                    if expired:
                        self._emit_protocol_snapshot()
            time.sleep(0.02)

    # ------------------ 内部 ------------------
    def _start_poll(self) -> None:
        self._stop.clear()
        now = time.monotonic()
        self._poll_started_at = now
        self._last_valid_at = now
        self._consecutive_read_errors = 0
        self._reported_faults.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            real_link = self._driver is not None and self._driver.is_open()
            read_failed = False
            try:
                if real_link:
                    frame = self._read_real_frame()
                else:
                    frame = self._make_simulated_frame()
                    frame.data_source = "sim"
                self._consecutive_read_errors = 0
            except Exception as exc:
                read_failed = True
                self.logMessage.emit(f"[警告] 读取异常：{exc}")
                self._consecutive_read_errors += 1
                if self._consecutive_read_errors >= 3:
                    self._report_fault_once(
                        "read_errors", f"通信连续读取异常（{self._consecutive_read_errors} 次）：{exc}")
                frame = None
            if frame is not None:
                self._last_valid_at = time.monotonic()
                self._latest_frame = frame
                self._inspect_frame_fault(frame)
                self.telemetryReceived.emit(frame)
            elif (not read_failed and real_link and self._kind != "以太网TCP" and
                  time.monotonic() - self._last_valid_at >= 2.0):
                self._report_fault_once("telemetry_timeout", "真实设备遥测连续 2 秒超时")
            time.sleep(0.1)

    def _inspect_frame_fault(self, frame: TelemetryFrame) -> None:
        if frame.bus_state == "ov":
            self._report_fault_once("bus_overvoltage", "直流母线过压跳闸")
        if frame.fault_code:
            self._report_fault_once(
                f"device_{frame.fault_code:02x}", frame.fault_text or
                f"下位机故障位 0x{frame.fault_code:02X}")

    def _report_fault_once(self, key: str, message: str) -> None:
        if key in self._reported_faults:
            return
        self._reported_faults.add(key)
        self.logMessage.emit(f"[错误] {message}")
        self.faultDetected.emit(message)

    def _read_real_frame(self) -> Optional[TelemetryFrame]:
        """从真实驱动读取并解析一帧遥测；解析不到则返回 None（保留上一帧）。"""
        if self._kind == "CAN总线":
            result = self._driver.recv_can(timeout=0.05)
            if result is None:
                return None
            arb_id, raw = result
            self.rawReceived.emit(arb_id, raw)
            if len(raw) < TELEM_LEN_CAN:
                return None
            return self._parse_can_raw(raw)
        # TCP：直接显示原始数据，不走帧解析
        if self._kind == "以太网TCP":
            chunk = self._driver.recv(size=256, timeout=0.05)
            if chunk:
                self.rawReceived.emit(0, chunk)
            return None
        # 串口：累计字节 → 在缓冲中扫描完整帧
        chunk = self._driver.recv(size=64, timeout=0.05)
        if chunk:
            self._rx_buf.extend(chunk)
        return self._try_extract_serial_frame()

    def _try_extract_serial_frame(self) -> Optional[TelemetryFrame]:
        """从 _rx_buf 中提取一个完整遥测帧。失败/不全时返回 None。"""
        buf = self._rx_buf
        while buf:
            head = buf.find(FRAME_HEADER)
            if head < 0:
                buf.clear()
                return None
            if head > 0:
                del buf[:head]
            if len(buf) < 5:
                return None  # 等待更多字节
            length = buf[2]
            total = length + 5
            if len(buf) < total:
                return None
            candidate = bytes(buf[:total])
            del buf[:total]
            decoded = decode_frame(candidate)
            if decoded is None:
                continue   # 校验失败，丢弃这一帧后继续找下一个 0xAA
            cmd, payload = decoded
            if cmd != CMD_TELEMETRY or len(payload) < TELEM_LEN:
                continue
            return self._parse_telemetry_payload(payload)
        return None

    def _parse_telemetry_payload(self, payload: bytes) -> TelemetryFrame:
        speed, spd_tgt, cur_ma, raw, ang_cdeg, temp_b, q, conv, flags = \
            struct.unpack_from(TELEM_FMT, payload)
        f = TelemetryFrame()
        f.speed_actual = float(speed)
        f.speed_target = float(spd_tgt)
        f.current_actual = cur_ma / TELEM_CURRENT_SCALE
        f.current_target = self._latest_frame.current_target
        f.angle_raw = float(raw)
        f.angle_actual = ang_cdeg / TELEM_ANGLE_SCALE
        f.temperature = float(temp_b) + TELEM_TEMP_OFFSET
        f.sensor_quality = q / 255.0
        f.convergence = conv / 255.0
        f.low_speed_warn = bool(flags & TELEM_FLAG_LOW_SPEED_WARN)
        fault_names = []
        if flags & TELEM_FLAG_OVERCURRENT_FAULT:
            fault_names.append("下位机过流保护")
        if flags & TELEM_FLAG_DRIVER_FAULT:
            fault_names.append("栅极驱动器故障")
        if flags & TELEM_FLAG_EMERGENCY_FAULT:
            fault_names.append("下位机急停/保护锁定")
        f.fault_code = flags & (
            TELEM_FLAG_OVERCURRENT_FAULT | TELEM_FLAG_DRIVER_FAULT |
            TELEM_FLAG_EMERGENCY_FAULT)
        f.fault_text = "；".join(fault_names)
        f.torque_actual = f.current_actual * TELEM_TORQUE_FROM_CURRENT
        f.torque_target = self._latest_frame.torque_target
        # 真机协议暂无母线字段，沿用上一帧（待协议扩展 CMD 后替换）
        f.vdc = self._latest_frame.vdc
        f.bus_state = self._latest_frame.bus_state
        f.sensor_source = self._active_sensor_name
        f.data_source = "real"
        return f

    def _parse_can_raw(self, data: bytes) -> TelemetryFrame:
        """CAN 单帧 8 字节裸 payload，仅含 4 个核心字段。"""
        speed, spd_tgt, cur_ma, raw = struct.unpack_from(TELEM_FMT_CAN, data)
        # 以最近一帧（仿真或真机）为底，覆盖 CAN 上来的字段
        f = TelemetryFrame()
        prev = self._latest_frame
        f.angle_actual = prev.angle_actual
        f.temperature = prev.temperature
        f.sensor_quality = prev.sensor_quality
        f.convergence = prev.convergence
        f.low_speed_warn = prev.low_speed_warn
        f.fault_code = prev.fault_code
        f.fault_text = prev.fault_text
        f.torque_target = prev.torque_target
        f.current_target = prev.current_target
        f.vdc = prev.vdc
        f.bus_state = prev.bus_state
        f.speed_actual = float(speed)
        f.speed_target = float(spd_tgt)
        f.current_actual = cur_ma / TELEM_CURRENT_SCALE
        f.angle_raw = float(raw)
        f.torque_actual = f.current_actual * TELEM_TORQUE_FROM_CURRENT
        f.sensor_source = self._active_sensor_name
        f.data_source = "real_partial"
        return f

    def start_simulation(self) -> None:
        """启动模拟数据流；电机保持静止，直到收到显式启动命令。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._motor_sim.reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_simulation(self) -> None:
        """停止并复位模拟对象，向所有可视化页面发布静止初始帧。"""
        if self._driver is not None and self._driver.is_open():
            return  # 真实连接时不允许停止轮询
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._motor_sim.reset()
        self._sim_t = 0.0
        self._bus_state_prev = "normal"
        self._latest_frame = TelemetryFrame()
        self.telemetryReceived.emit(self._latest_frame)

    def _make_simulated_frame(self) -> TelemetryFrame:
        """步进 PMSM 物理模型 0.1 s，采样为一帧遥测（数字孪生 L1）。"""
        self._sim_t += 0.1
        sim = self._motor_sim
        sim.step(0.1)
        f = TelemetryFrame()
        f.speed_target = sim.speed_ref_rpm if sim.enabled else 0.0
        moving = sim.enabled or abs(sim.speed_rpm) >= 0.1
        f.speed_actual = sim.speed_rpm + (random.uniform(-2.0, 2.0) if moving else 0.0)
        f.current_target = sim.iq_ref
        f.current_actual = sim.i_q + (random.uniform(-0.03, 0.03) if moving else 0.0)
        f.torque_target = sim.torque_ref
        f.torque_actual = sim.torque
        f.temperature = sim.temp
        f.vdc = sim.vdc
        f.bus_state = sim.bus_state
        if sim.bus_state == "ov":
            f.fault_code = 0x100
            f.fault_text = "数字孪生直流母线过压跳闸"
        f.powers = {
            "supply": sim.p_supply, "loss_src": sim.p_loss_src,
            "brake": sim.p_brake, "inv": sim.p_inv, "cu": sim.p_cu,
            "em": sim.p_em, "fric": sim.p_fric, "kinetic": sim.p_kinetic,
        }
        self._log_bus_transition(f.bus_state, sim.vdc)
        base_angle = sim.angle_deg
        f.angle_actual = base_angle
        f.sensor_source = self._active_sensor_name
        f.data_source = "sim"
        self._fill_sensor_specific(f, base_angle)
        return f

    def _log_bus_transition(self, state: str, vdc: float) -> None:
        """母线状态跳变时写一条日志（斩波动作/欠压/过压跳闸）。"""
        prev = getattr(self, "_bus_state_prev", "normal")
        if state == prev:
            return
        self._bus_state_prev = state
        text = {
            "brake": f"[母线] 回馈泵升，制动斩波器动作（Vdc={vdc:.1f} V）",
            "uv": f"[母线] 欠压告警（Vdc={vdc:.1f} V）",
            "ov": f"[母线] 过压跳闸，逆变器封管保护（Vdc={vdc:.1f} V）",
            "normal": f"[母线] 恢复正常（Vdc={vdc:.1f} V）",
        }.get(state)
        if text:
            self.logMessage.emit(text)

    def _fill_sensor_specific(self, f: TelemetryFrame, base_angle: float) -> None:
        """按当前活跃传感器，注入差异化的角度/质量/收敛/原始量。"""
        sid = self._active_sensor_id
        speed = f.speed_actual

        if sid == 0:  # Hall：60° 离散量化真实角度，质量较低，但低速换相稳定
            step = int(base_angle // 60.0) % 6
            f.angle_actual = step * 60.0
            f.angle_raw = float(step)
            f.sensor_quality = 0.7 + random.uniform(-0.05, 0.05)
            f.convergence = 1.0
            f.low_speed_warn = False

        elif sid == 1:  # QEP：高分辨率角度，原始量为真实角度对应的脉冲计数
            f.angle_actual = base_angle + random.uniform(-0.05, 0.05)
            f.angle_raw = float(int(base_angle / 360.0 * 2500.0) % 2500)
            f.sensor_quality = 0.99
            f.convergence = 1.0
            f.low_speed_warn = False

        elif sid == 2:  # Resolver：平滑+小噪声，质量随激励信号波动
            f.angle_actual = base_angle + random.uniform(-0.2, 0.2)
            f.angle_raw = f.angle_actual
            f.sensor_quality = 0.95 + 0.03 * math.sin(self._sim_t * 0.3)
            f.convergence = 1.0
            f.low_speed_warn = False

        elif sid == 3:  # SMO：估算角度有较大抖动，低速段不可用
            f.angle_actual = base_angle + 5.0 * math.sin(self._sim_t * 3.0)
            f.angle_raw = f.angle_actual
            f.sensor_quality = max(0.0, 0.6 + 0.2 * math.sin(self._sim_t))
            f.convergence = min(1.0, (self._sim_t % 20.0) / 10.0)
            f.low_speed_warn = speed < 80.0

        elif sid == 4:  # EKF：噪声更小、收敛更快
            f.angle_actual = base_angle + 2.0 * math.sin(self._sim_t * 2.5)
            f.angle_raw = f.angle_actual
            f.sensor_quality = 0.85 + 0.05 * math.sin(self._sim_t * 0.5)
            f.convergence = min(1.0, (self._sim_t % 15.0) / 8.0)
            f.low_speed_warn = speed < 60.0

        elif sid == 5:  # MRAS：中等噪声，对参数漂移敏感
            f.angle_actual = base_angle + 3.0 * math.sin(self._sim_t * 2.0)
            f.angle_raw = f.angle_actual
            f.sensor_quality = 0.75 + 0.1 * math.sin(self._sim_t)
            f.convergence = min(1.0, (self._sim_t % 18.0) / 10.0)
            f.low_speed_warn = speed < 70.0

        elif sid == 6:  # HFI：低速好高速差，blend_speed 以下需注入
            blend = 100.0
            f.angle_actual = base_angle + 1.5 * math.sin(self._sim_t * 4.0)
            # 原始量记录注入残余
            f.angle_raw = 5.0 * math.sin(self._sim_t * 1_000.0 * 0.001)
            f.sensor_quality = 0.9 if speed > blend else 0.4
            f.convergence = min(1.0, speed / max(1.0, blend))
            f.low_speed_warn = speed < blend
        else:
            f.sensor_quality = 1.0
            f.convergence = 1.0
            f.low_speed_warn = False
