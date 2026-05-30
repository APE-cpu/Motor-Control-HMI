"""通信管理器：上层统一入口。

- 维护当前的通信驱动（串口或 CAN）
- 提供连接 / 断开 / 发送 / 接收
- 在后台线程上轮询数据，通过 Qt 信号通知上层
- 当未连接时，提供模拟数据源，便于在没有真实下位机时进行 UI 演示
"""
import math
import random
import struct
import threading
import time
from typing import Optional, Tuple

from PySide6.QtCore import QObject, Signal

from config.config import (
    CMD_TELEMETRY, FRAME_HEADER, FRAME_TAIL,
    TELEM_ANGLE_SCALE, TELEM_CURRENT_SCALE, TELEM_FMT, TELEM_FMT_CAN,
    TELEM_LEN, TELEM_LEN_CAN, TELEM_TEMP_OFFSET, TELEM_TORQUE_FROM_CURRENT,
)

from .base_comm import BaseComm
from .can_comm import CANComm
from .protocol import decode_frame
from .serial_comm import SerialComm
from .tcp_comm import TCPComm
from .zlgcan_comm import ZlgCanComm
from .zlgcan_zcan_comm import ZlgCanZcanComm


class TelemetryFrame:
    """一帧遥测数据（监控页面消费）。"""

    __slots__ = (
        "speed_actual", "speed_target",
        "current_actual", "current_target",
        "torque_actual", "torque_target",
        "angle_actual", "temperature",
        # 传感器差异化字段
        "sensor_source",   # 当前活跃传感器名称
        "sensor_quality",  # 0-1 之间的数据质量/置信度
        "angle_raw",       # 传感器原始量（Hall=离散步、QEP=脉冲计数、其它=估算值）
        "convergence",     # 无传感器观测器收敛度 0-1；有传感器时恒为 1.0
        "low_speed_warn",  # 低速段是否进入不可用区
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
        self.sensor_source = ""
        self.sensor_quality = 1.0
        self.angle_raw = 0.0
        self.convergence = 1.0
        self.low_speed_warn = False
        self.data_source = "sim"


class CommManager(QObject):
    """通信管理器（线程安全），通过 Qt 信号将数据推给 UI。"""

    statusChanged = Signal(bool, str)        # 连接状态 + 备注
    telemetryReceived = Signal(object)        # TelemetryFrame
    logMessage = Signal(str)                  # 日志/错误信息
    rawReceived = Signal(int, bytes)          # CAN原始帧 (arbitration_id, data)

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
        self._sim_pulse_count: int = 0
        self._rx_buf = bytearray()
        self._latest_frame = TelemetryFrame()

    # ------------------ 公共接口 ------------------
    def connect(self, kind: str, **cfg) -> bool:
        """kind: "RS-232" / "RS-485" / "CAN总线"。"""
        self.disconnect()
        try:
            if kind in ("RS-232", "RS-485"):
                self._driver = SerialComm()
                self._driver.open(**cfg)
            elif kind == "CAN总线":
                # interface 决定后端：
                #   zlgcan       -> 创芯 ControlCAN.dll（VCI_* 老接口）
                #   zlgcan-zcan  -> 致远原厂 zlgcan.dll（ZCAN_* 新接口）
                #   其余          -> python-can
                iface = str(cfg.get("interface", "")).lower()
                if iface == "zlgcan":
                    self._driver = ZlgCanComm()
                elif iface == "zlgcan-zcan":
                    self._driver = ZlgCanZcanComm()
                else:
                    self._driver = CANComm()
                self._driver.open(**cfg)
            elif kind == "以太网TCP":
                self._driver = TCPComm()
                self._driver.open(**cfg)
            else:
                raise ValueError(f"未知通信方式: {kind}")
            self._kind = kind
            self._cfg = cfg
            self._start_poll()
            self.statusChanged.emit(True, f"{kind} 已连接")
            self.logMessage.emit(f"[通信] {kind} 已连接，参数={cfg}")
            return True
        except Exception as exc:
            self.logMessage.emit(f"[错误] 连接失败：{exc}")
            self.statusChanged.emit(False, str(exc))
            self._driver = None
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
        self._rx_buf.clear()
        self.statusChanged.emit(False, "已断开")

    def is_connected(self) -> bool:
        return self._driver is not None and self._driver.is_open()

    def set_active_sensor(self, sensor_id: int, sensor_name: str = "") -> None:
        self._active_sensor_id = int(sensor_id)
        if sensor_name:
            self._active_sensor_name = sensor_name

    def send_frame(self, data: bytes) -> bool:
        if not self.is_connected():
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
        if not self.is_connected():
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

    # ------------------ 内部 ------------------
    def _start_poll(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._driver is not None and self._driver.is_open():
                    frame = self._read_real_frame()
                else:
                    frame = self._make_simulated_frame()
                    frame.data_source = "sim"
            except Exception as exc:
                self.logMessage.emit(f"[警告] 读取异常：{exc}")
                frame = None
            if frame is not None:
                self._latest_frame = frame
                self.telemetryReceived.emit(frame)
            time.sleep(0.1)

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
        f.low_speed_warn = bool(flags & 0x01)
        f.torque_actual = f.current_actual * TELEM_TORQUE_FROM_CURRENT
        f.torque_target = self._latest_frame.torque_target
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
        f.torque_target = prev.torque_target
        f.current_target = prev.current_target
        f.speed_actual = float(speed)
        f.speed_target = float(spd_tgt)
        f.current_actual = cur_ma / TELEM_CURRENT_SCALE
        f.angle_raw = float(raw)
        f.torque_actual = f.current_actual * TELEM_TORQUE_FROM_CURRENT
        f.sensor_source = self._active_sensor_name
        f.data_source = "real_partial"
        return f

    def start_simulation(self) -> None:
        """启动模拟数据流。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_simulation(self) -> None:
        """停止模拟数据流（不影响真实连接）。"""
        if self._driver is not None and self._driver.is_open():
            return  # 真实连接时不允许停止轮询
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _make_simulated_frame(self) -> TelemetryFrame:
        self._sim_t += 0.1
        f = TelemetryFrame()
        f.speed_target = 1500.0
        f.speed_actual = 1500.0 + 30.0 * math.sin(self._sim_t) + random.uniform(-5, 5)
        f.current_target = 5.0
        f.current_actual = 5.0 + 0.3 * math.sin(self._sim_t * 1.7)
        f.torque_target = 2.0
        f.torque_actual = 2.0 + 0.1 * math.cos(self._sim_t * 1.3)
        f.temperature = 35.0 + 10.0 * (0.5 + 0.5 * math.sin(self._sim_t * 0.05))
        # 默认（QEP）的角度：连续平滑
        base_angle = (self._sim_t * 30.0) % 360.0
        f.angle_actual = base_angle
        f.sensor_source = self._active_sensor_name
        f.data_source = "sim"
        self._fill_sensor_specific(f, base_angle)
        return f

    def _fill_sensor_specific(self, f: TelemetryFrame, base_angle: float) -> None:
        """按当前活跃传感器，注入差异化的角度/质量/收敛/原始量。"""
        sid = self._active_sensor_id
        speed = f.speed_actual

        if sid == 0:  # Hall：60° 离散，质量较低，但低速换相稳定
            step = int(self._sim_t * 10) % 6
            f.angle_actual = step * 60.0
            f.angle_raw = float(step)
            f.sensor_quality = 0.7 + random.uniform(-0.05, 0.05)
            f.convergence = 1.0
            f.low_speed_warn = False

        elif sid == 1:  # QEP：高分辨率角度，原始量为脉冲计数
            f.angle_actual = base_angle + random.uniform(-0.05, 0.05)
            self._sim_pulse_count = (self._sim_pulse_count + 21) % 2500
            f.angle_raw = float(self._sim_pulse_count)
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
