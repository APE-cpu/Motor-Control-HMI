"""致远电子(ZLG)原厂 zlgcan.dll 通信封装（ZCAN_* 新接口）。

适用设备：USBCAN-II（以及其它走 zlgcan.dll 的致远设备，经典 CAN）。
与同目录下 zlgcan_comm.py（创芯 ControlCAN，VCI_* 老接口）互不通用，
本类对接的是致远官方较新的 zlgcan.dll，函数名为 ZCAN_*。

DLL 获取与放置：
- 致远官网下载的 zlgcan 包内含 zlgcan.dll 与 kerneldlls 目录。
- 32 位 / 64 位 DLL 必须与运行 Python 的位数一致，否则报
  “不是有效的 Win32 应用程序 / [WinError 193]”。本项目为 64 位 Python，
  务必使用 x64 的 zlgcan.dll + x64 kerneldlls。
- zlgcan.dll 在打开设备时会按“当前工作目录”去加载 kerneldlls 与
  devices_property 配置。本类在 open() 期间临时切换到 dll 所在目录，
  确保 kerneldlls 能被找到，open 结束后恢复原工作目录。

默认在以下位置查找 zlgcan.dll（取第一个存在者）：
- 环境变量 ZLGCAN_DIR 指向的目录
- 项目根/zlgcan_x64
- exe 同级/zlgcan_x64
- 随包资源（PyInstaller _MEIPASS）/zlgcan_x64
"""
import contextlib
import ctypes
import os
from ctypes import (
    POINTER, Structure, Union, byref, c_char, c_char_p, c_int, c_ubyte,
    c_uint, c_uint64, c_ushort, c_void_p, create_string_buffer,
)
from pathlib import Path
from typing import Optional, Tuple

from .base_comm import BaseComm
from runtime_paths import app_base_dir, resource_path

# ---- 设备类型常量（zlgcan.h 标准定义）----
ZCAN_USBCAN1 = 3          # USBCAN-I
ZCAN_USBCAN2 = 4          # USBCAN-II（同学的设备）

# ZCAN 调用成功返回 1（STATUS_OK）
_STATUS_OK = 1
_INVALID_HANDLE = 0

# 帧类型（ZCAN_GetReceiveNum 的参数）
_TYPE_CAN = 0

# can_id 标志位（见 canframe.h）
_CAN_EFF_FLAG = 0x80000000   # 扩展帧
_CAN_RTR_FLAG = 0x40000000   # 远程帧
_CAN_ID_MASK = 0x1FFFFFFF

# 经典 CAN 支持的波特率（zlgcan 用字符串配置，无需 Timing 寄存器）
_SUPPORTED_BAUD = (
    1_000_000, 800_000, 500_000, 250_000,
    125_000, 100_000, 50_000, 20_000, 10_000,
)

# 放在 dll 同级、需要被定位的子目录名（仅用于自检提示）
_KERNEL_DIR = "kerneldlls"


# ============ ctypes 结构体（对齐 zlgcan.h / canframe.h）============
class _CanFrame(Structure):
    """canframe.h: can_frame。"""
    _fields_ = [
        ("can_id", c_uint),         # 32 位：bit31=EFF, bit30=RTR, bit29=ERR, 低位=ID
        ("can_dlc", c_ubyte),       # 数据长度 0..8
        ("__pad", c_ubyte),
        ("__res0", c_ubyte),
        ("__res1", c_ubyte),
        ("data", c_ubyte * 8),
    ]


class _ZcanTransmitData(Structure):
    """zlgcan.h: ZCAN_Transmit_Data。"""
    _fields_ = [
        ("frame", _CanFrame),
        ("transmit_type", c_uint),  # 0=正常发送
    ]


class _ZcanReceiveData(Structure):
    """zlgcan.h: ZCAN_Receive_Data。"""
    _fields_ = [
        ("frame", _CanFrame),
        ("timestamp", c_uint64),    # us
    ]


class _CanCfg(Structure):
    _fields_ = [
        ("acc_code", c_uint),
        ("acc_mask", c_uint),
        ("reserved", c_uint),
        ("filter", c_ubyte),
        ("timing0", c_ubyte),
        ("timing1", c_ubyte),
        ("mode", c_ubyte),
    ]


class _CanFdCfg(Structure):
    _fields_ = [
        ("acc_code", c_uint),
        ("acc_mask", c_uint),
        ("abit_timing", c_uint),
        ("dbit_timing", c_uint),
        ("brp", c_uint),
        ("filter", c_ubyte),
        ("mode", c_ubyte),
        ("pad", c_ushort),
        ("reserved", c_uint),
    ]


class _CfgUnion(Union):
    _fields_ = [("can", _CanCfg), ("canfd", _CanFdCfg)]


class _ZcanChannelInitConfig(Structure):
    """zlgcan.h: ZCAN_CHANNEL_INIT_CONFIG。"""
    _fields_ = [
        ("can_type", c_uint),       # 0=TYPE_CAN（USBCAN-II 固定经典 CAN）
        ("config", _CfgUnion),
    ]


class _ZcanDeviceInfo(Structure):
    """zlgcan.h: ZCAN_DEVICE_INFO。"""
    _fields_ = [
        ("hw_Version", c_ushort),
        ("fw_Version", c_ushort),
        ("dr_Version", c_ushort),
        ("in_Version", c_ushort),
        ("irq_Num", c_ushort),
        ("can_Num", c_ubyte),
        ("str_Serial_Num", c_ubyte * 20),
        ("str_hw_Type", c_ubyte * 40),
        ("reserved", c_ushort * 4),
    ]


def _find_dll_dir() -> Path:
    """按优先级定位含 zlgcan.dll 的目录。"""
    candidates = []
    env = os.environ.get("ZLGCAN_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [
        app_base_dir() / "zlgcan_x64",
        resource_path("zlgcan_x64"),
        app_base_dir(),                 # 兜底：dll 直接放根目录
    ]
    for d in candidates:
        try:
            if (d / "zlgcan.dll").is_file():
                return d
        except OSError:
            continue
    # 没找到也返回首选，交给加载阶段报明确错误
    return app_base_dir() / "zlgcan_x64"


class ZlgCanZcanComm(BaseComm):
    """致远原厂 zlgcan.dll（ZCAN_* 接口）。"""

    name = "ZLGCAN-ZCAN"

    def __init__(self) -> None:
        self._dll: Optional[ctypes.WinDLL] = None
        self._dll_dir: Optional[Path] = None
        self._dev: int = _INVALID_HANDLE        # DEVICE_HANDLE（void*，x64 为 64 位）
        self._chn: int = _INVALID_HANDLE        # CHANNEL_HANDLE
        self._devtype = ZCAN_USBCAN2
        self._devindex = 0
        self._chn_index = 0
        self._open = False

    # ---------------- DLL 加载与签名 ----------------
    def _load_dll(self) -> None:
        self._dll_dir = _find_dll_dir()
        dll_path = self._dll_dir / "zlgcan.dll"
        if not dll_path.is_file():
            raise RuntimeError(
                f"未找到 zlgcan.dll（查找目录：{self._dll_dir}）。"
                "请放置 x64 的 zlgcan.dll 及其 kerneldlls 目录，"
                "或设置环境变量 ZLGCAN_DIR 指向所在文件夹。"
            )
        if not (self._dll_dir / _KERNEL_DIR).is_dir():
            raise RuntimeError(
                f"zlgcan.dll 同级缺少 {_KERNEL_DIR} 目录（{self._dll_dir}）。"
                "kerneldlls 必须与 zlgcan.dll 放在一起。"
            )
        # 让依赖的 kerneldlls 能被解析（Python 3.8+ 不再默认搜当前目录）
        with contextlib.suppress(OSError, AttributeError):
            os.add_dll_directory(str(self._dll_dir))
        try:
            self._dll = ctypes.WinDLL(str(dll_path))   # zlgcan 为 __stdcall
        except OSError as e:
            raise RuntimeError(
                f"加载 zlgcan.dll 失败：{e}。请确认 DLL 位数(应为 x64)与 "
                "Python 一致。"
            )
        self._bind_signatures()

    def _bind_signatures(self) -> None:
        d = self._dll
        # 句柄是指针：x64 上必须 c_void_p，用 c_uint 会截断高 32 位导致崩溃
        d.ZCAN_OpenDevice.argtypes = [c_uint, c_uint, c_uint]
        d.ZCAN_OpenDevice.restype = c_void_p
        d.ZCAN_CloseDevice.argtypes = [c_void_p]
        d.ZCAN_CloseDevice.restype = c_uint
        d.ZCAN_GetDeviceInf.argtypes = [c_void_p, POINTER(_ZcanDeviceInfo)]
        d.ZCAN_GetDeviceInf.restype = c_uint
        d.ZCAN_InitCAN.argtypes = [c_void_p, c_uint, POINTER(_ZcanChannelInitConfig)]
        d.ZCAN_InitCAN.restype = c_void_p
        d.ZCAN_StartCAN.argtypes = [c_void_p]
        d.ZCAN_StartCAN.restype = c_uint
        d.ZCAN_ResetCAN.argtypes = [c_void_p]
        d.ZCAN_ResetCAN.restype = c_uint
        d.ZCAN_ClearBuffer.argtypes = [c_void_p]
        d.ZCAN_ClearBuffer.restype = c_uint
        d.ZCAN_GetReceiveNum.argtypes = [c_void_p, c_ubyte]
        d.ZCAN_GetReceiveNum.restype = c_uint
        d.ZCAN_Transmit.argtypes = [c_void_p, POINTER(_ZcanTransmitData), c_uint]
        d.ZCAN_Transmit.restype = c_uint
        d.ZCAN_Receive.argtypes = [c_void_p, POINTER(_ZcanReceiveData), c_uint, c_int]
        d.ZCAN_Receive.restype = c_uint
        d.ZCAN_SetValue.argtypes = [c_void_p, c_char_p, c_void_p]
        d.ZCAN_SetValue.restype = c_uint

    @contextlib.contextmanager
    def _in_dll_dir(self):
        """临时切到 dll 目录，让 zlgcan 能按相对路径加载 kerneldlls/配置。"""
        prev = os.getcwd()
        try:
            if self._dll_dir is not None:
                os.chdir(str(self._dll_dir))
            yield
        finally:
            with contextlib.suppress(OSError):
                os.chdir(prev)

    # ---------------- BaseComm 接口 ----------------
    def open(self, device_type: int = ZCAN_USBCAN2, device_index: int = 0,
             can_channel: int = 0, bitrate: int = 500_000, **_) -> bool:
        self._load_dll()
        self._devtype = int(device_type)
        self._devindex = int(device_index)
        self._chn_index = int(can_channel)

        if int(bitrate) not in _SUPPORTED_BAUD:
            raise RuntimeError(
                f"不支持的波特率 {bitrate}，可选：{sorted(_SUPPORTED_BAUD)}"
            )

        with self._in_dll_dir():
            self._dev = self._dll.ZCAN_OpenDevice(
                self._devtype, self._devindex, 0) or _INVALID_HANDLE
            if self._dev == _INVALID_HANDLE:
                raise RuntimeError(
                    f"打开 ZLG 设备失败（类型={self._devtype} 索引={self._devindex}）。"
                    "请确认：设备已插好、致远驱动已安装、设备类型号正确、"
                    "未被其它程序（如 ZCANPRO）占用。"
                )

            # 波特率：致远新接口用字符串配置，InitCAN 前设置（路径："通道/baud_rate"）
            baud = str(int(bitrate)).encode("ascii")
            path = f"{self._chn_index}/baud_rate".encode("ascii")
            if self._dll.ZCAN_SetValue(self._dev, path, baud) != _STATUS_OK:
                self._safe_close_device()
                raise RuntimeError(
                    f"设置波特率失败（{bitrate}）。请确认设备支持该波特率。"
                )

            cfg = _ZcanChannelInitConfig()
            cfg.can_type = _TYPE_CAN          # USBCAN-II 固定经典 CAN
            cfg.config.can.acc_code = 0x00000000
            cfg.config.can.acc_mask = 0xFFFFFFFF   # 接收所有 ID
            cfg.config.can.reserved = 0
            cfg.config.can.filter = 1              # 单滤波
            cfg.config.can.timing0 = 0             # 已用 SetValue 配波特率，留 0
            cfg.config.can.timing1 = 0
            cfg.config.can.mode = 0                # 0=正常模式

            self._chn = self._dll.ZCAN_InitCAN(
                self._dev, self._chn_index, byref(cfg)) or _INVALID_HANDLE
            if self._chn == _INVALID_HANDLE:
                self._safe_close_device()
                raise RuntimeError("初始化 CAN 通道失败（ZCAN_InitCAN）。")

            if self._dll.ZCAN_StartCAN(self._chn) != _STATUS_OK:
                self._safe_close_device()
                raise RuntimeError("启动 CAN 通道失败（ZCAN_StartCAN）。")

            self._dll.ZCAN_ClearBuffer(self._chn)

        self._open = True
        return True

    def _safe_close_device(self) -> None:
        if self._dll is not None and self._dev != _INVALID_HANDLE:
            with contextlib.suppress(Exception):
                self._dll.ZCAN_CloseDevice(self._dev)
        self._dev = _INVALID_HANDLE
        self._chn = _INVALID_HANDLE

    def close(self) -> None:
        if self._dll is not None and self._open:
            with contextlib.suppress(Exception):
                if self._chn != _INVALID_HANDLE:
                    self._dll.ZCAN_ResetCAN(self._chn)
                if self._dev != _INVALID_HANDLE:
                    self._dll.ZCAN_CloseDevice(self._dev)
        self._open = False
        self._dev = _INVALID_HANDLE
        self._chn = _INVALID_HANDLE
        self._dll = None

    def is_open(self) -> bool:
        return self._open and self._dll is not None and self._chn != _INVALID_HANDLE

    def send(self, data: bytes, arbitration_id: int = 0x100,
             is_extended: bool = False) -> int:
        if not self.is_open():
            raise RuntimeError("ZLG CAN(ZCAN) 未打开")
        tx = _ZcanTransmitData()
        can_id = int(arbitration_id) & _CAN_ID_MASK
        if is_extended:
            can_id |= _CAN_EFF_FLAG
        tx.frame.can_id = can_id
        tx.transmit_type = 0          # 0=正常发送
        n = min(len(data), 8)
        tx.frame.can_dlc = n
        for i in range(n):
            tx.frame.data[i] = data[i]
        sent = self._dll.ZCAN_Transmit(self._chn, byref(tx), 1)
        if sent != 1:
            raise RuntimeError("CAN 发送失败（ZCAN_Transmit）")
        return n

    def recv(self, size: int = 8, timeout: Optional[float] = 0.05) -> bytes:
        """与 BaseComm 对齐：仅返回数据字节（丢弃 ID）。"""
        result = self.recv_can(timeout=timeout)
        if result is None:
            return b""
        return result[1][:size]

    def recv_can(self, timeout: float = 0.05) -> Optional[Tuple[int, bytes]]:
        """读取一帧 CAN，返回 (arbitration_id, data) 或 None。"""
        if not self.is_open():
            return None
        if self._dll.ZCAN_GetReceiveNum(self._chn, _TYPE_CAN) == 0:
            return None
        buf = (_ZcanReceiveData * 1)()
        wait_ms = max(0, int((timeout or 0) * 1000))
        n = self._dll.ZCAN_Receive(self._chn, buf, 1, wait_ms)
        if n == 0 or n == 0xFFFFFFFF:
            return None
        frame = buf[0].frame
        dlen = min(frame.can_dlc, 8)
        arb_id = frame.can_id & _CAN_ID_MASK
        return int(arb_id), bytes(frame.data[:dlen])
