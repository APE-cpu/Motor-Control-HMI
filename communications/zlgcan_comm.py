"""致远电子(ZLG) USBCAN 设备通信封装（ControlCAN.dll）。

适用设备：USBCAN-I / USBCAN-II，以及兼容 ControlCAN 接口的创芯 CANalyst-II。
这类设备 python-can 默认不支持，需调用厂商提供的 ControlCAN.dll。

DLL 获取与放置：
- 从 ZLG 官网或 CANalyst-II 配套光盘获取 ControlCAN.dll（以及随附的
  kerneldlls 目录，如 usbcan.dll 等需与 ControlCAN.dll 放在同一目录）。
- 32 位 / 64 位 DLL 必须与运行 Python 的位数一致，否则报
  “不是有效的 Win32 应用程序 / [WinError 193]”。
- 将 DLL 放到上位机程序根目录（exe 同级）即可被自动加载。

新款支持 CANFD 的设备（USBCANFD / CANFDNET 等）使用的是 zlgcan.dll
（ZCAN_ 系列 API），与此处的 ControlCAN 接口不通用，需另行适配。
"""
import ctypes
from ctypes import POINTER, byref, c_ubyte, c_uint, c_char
from typing import Optional, Tuple

from .base_comm import BaseComm
from runtime_paths import app_base_dir, resource_path

# ---- 设备类型常量（ControlCAN 标准定义）----
VCI_USBCAN1 = 3          # USBCAN-I
VCI_USBCAN2 = 4          # USBCAN-II / CANalyst-II（创芯兼容）

# ControlCAN 调用成功返回 1
_STATUS_OK = 1

# ---- 波特率 → (Timing0, Timing1)，16MHz 时钟标准配置 ----
_TIMING = {
    1_000_000: (0x00, 0x14),
    800_000:   (0x00, 0x16),
    500_000:   (0x00, 0x1C),
    250_000:   (0x01, 0x1C),
    125_000:   (0x03, 0x1C),
    100_000:   (0x04, 0x1C),
    50_000:    (0x09, 0x1C),
    20_000:    (0x18, 0x1C),
    10_000:    (0x31, 0x1C),
}


class VCI_INIT_CONFIG(ctypes.Structure):
    _fields_ = [
        ("AccCode", c_uint),
        ("AccMask", c_uint),
        ("Reserved", c_uint),
        ("Filter", c_ubyte),
        ("Timing0", c_ubyte),
        ("Timing1", c_ubyte),
        ("Mode", c_ubyte),
    ]


class VCI_CAN_OBJ(ctypes.Structure):
    _fields_ = [
        ("ID", c_uint),
        ("TimeStamp", c_uint),
        ("TimeFlag", c_ubyte),
        ("SendType", c_ubyte),
        ("RemoteFlag", c_ubyte),
        ("ExternFlag", c_ubyte),
        ("DataLen", c_ubyte),
        ("Data", c_ubyte * 8),
        ("Reserved", c_ubyte * 3),
    ]


def _load_dll() -> ctypes.WinDLL:
    """按优先级查找并加载 ControlCAN.dll（stdcall）。"""
    candidates = [
        resource_path("ControlCAN.dll"),          # 随包资源 / _MEIPASS
        app_base_dir() / "ControlCAN.dll",         # exe 同级目录
        "ControlCAN.dll",                          # 交给系统按 PATH 搜索
    ]
    last_err: Optional[Exception] = None
    for c in candidates:
        try:
            return ctypes.WinDLL(str(c))
        except OSError as e:
            last_err = e
    raise RuntimeError(
        "未找到或无法加载 ControlCAN.dll。请将其放到程序根目录，"
        "并确认 DLL 位数(32/64)与 Python 一致。"
        f"（最后错误：{last_err}）"
    )


class ZlgCanComm(BaseComm):
    """ZLG USBCAN 驱动（ControlCAN.dll）。"""

    name = "ZLGCAN"

    def __init__(self) -> None:
        self._dll: Optional[ctypes.WinDLL] = None
        self._devtype = VCI_USBCAN2
        self._devindex = 0
        self._chn = 0
        self._open = False

    def _bind_signatures(self) -> None:
        d = self._dll
        d.VCI_OpenDevice.argtypes = [c_uint, c_uint, c_uint]
        d.VCI_OpenDevice.restype = c_uint
        d.VCI_CloseDevice.argtypes = [c_uint, c_uint]
        d.VCI_CloseDevice.restype = c_uint
        d.VCI_InitCAN.argtypes = [c_uint, c_uint, c_uint, POINTER(VCI_INIT_CONFIG)]
        d.VCI_InitCAN.restype = c_uint
        d.VCI_StartCAN.argtypes = [c_uint, c_uint, c_uint]
        d.VCI_StartCAN.restype = c_uint
        d.VCI_ResetCAN.argtypes = [c_uint, c_uint, c_uint]
        d.VCI_ResetCAN.restype = c_uint
        d.VCI_ClearBuffer.argtypes = [c_uint, c_uint, c_uint]
        d.VCI_ClearBuffer.restype = c_uint
        d.VCI_Transmit.argtypes = [c_uint, c_uint, c_uint, POINTER(VCI_CAN_OBJ), c_uint]
        d.VCI_Transmit.restype = c_uint
        d.VCI_Receive.argtypes = [c_uint, c_uint, c_uint, POINTER(VCI_CAN_OBJ), c_uint, ctypes.c_int]
        d.VCI_Receive.restype = c_uint

    def open(self, device_type: int = VCI_USBCAN2, device_index: int = 0,
             can_channel: int = 0, bitrate: int = 500_000, **_) -> bool:
        self._dll = _load_dll()
        self._bind_signatures()
        self._devtype = int(device_type)
        self._devindex = int(device_index)
        self._chn = int(can_channel)

        if int(bitrate) not in _TIMING:
            raise RuntimeError(
                f"不支持的波特率 {bitrate}，可选：{sorted(_TIMING)}"
            )
        t0, t1 = _TIMING[int(bitrate)]

        if self._dll.VCI_OpenDevice(self._devtype, self._devindex, 0) != _STATUS_OK:
            raise RuntimeError(
                f"打开 ZLG 设备失败（类型={self._devtype} 索引={self._devindex}）。"
                "请确认设备已插好、驱动已安装、设备类型号正确。"
            )

        cfg = VCI_INIT_CONFIG(
            AccCode=0x00000000,
            AccMask=0xFFFFFFFF,   # 接收所有 ID
            Reserved=0,
            Filter=1,             # 单滤波
            Timing0=t0,
            Timing1=t1,
            Mode=0,               # 正常模式（0=正常,1=只听,2=自发自收）
        )
        if self._dll.VCI_InitCAN(self._devtype, self._devindex, self._chn, byref(cfg)) != _STATUS_OK:
            self._dll.VCI_CloseDevice(self._devtype, self._devindex)
            raise RuntimeError("初始化 CAN 通道失败（VCI_InitCAN）。")

        if self._dll.VCI_StartCAN(self._devtype, self._devindex, self._chn) != _STATUS_OK:
            self._dll.VCI_CloseDevice(self._devtype, self._devindex)
            raise RuntimeError("启动 CAN 通道失败（VCI_StartCAN）。")

        self._dll.VCI_ClearBuffer(self._devtype, self._devindex, self._chn)
        self._open = True
        return True

    def close(self) -> None:
        if self._dll is not None and self._open:
            try:
                self._dll.VCI_ResetCAN(self._devtype, self._devindex, self._chn)
                self._dll.VCI_CloseDevice(self._devtype, self._devindex)
            except Exception:
                pass
        self._open = False
        self._dll = None

    def is_open(self) -> bool:
        return self._open and self._dll is not None

    def send(self, data: bytes, arbitration_id: int = 0x100) -> int:
        if not self.is_open():
            raise RuntimeError("ZLG CAN 未打开")
        obj = VCI_CAN_OBJ()
        obj.ID = int(arbitration_id)
        obj.SendType = 0          # 0=正常发送
        obj.RemoteFlag = 0        # 0=数据帧
        obj.ExternFlag = 0        # 0=标准帧（11位ID）
        n = min(len(data), 8)
        obj.DataLen = n
        for i in range(n):
            obj.Data[i] = data[i]
        ret = self._dll.VCI_Transmit(self._devtype, self._devindex, self._chn, byref(obj), 1)
        if ret != 1:
            raise RuntimeError("CAN 发送失败（VCI_Transmit）")
        return n

    def recv(self, size: int = 8, timeout: Optional[float] = 0.05) -> bytes:
        """与 BaseComm 接口对齐：仅返回数据字节（丢弃 ID）。"""
        result = self.recv_can(timeout=timeout)
        if result is None:
            return b""
        return result[1][:size]

    def recv_can(self, timeout: float = 0.05) -> Optional[Tuple[int, bytes]]:
        """读取一帧 CAN，返回 (arbitration_id, data) 或 None。"""
        if not self.is_open():
            return None
        buf = (VCI_CAN_OBJ * 1)()
        wait_ms = max(0, int((timeout or 0) * 1000))
        n = self._dll.VCI_Receive(self._devtype, self._devindex, self._chn,
                                  buf, 1, wait_ms)
        # 0 表示无数据；0xFFFFFFFF(=-1) 表示读取错误
        if n == 0 or n == 0xFFFFFFFF:
            return None
        obj = buf[0]
        dlen = min(obj.DataLen, 8)
        return int(obj.ID), bytes(obj.Data[:dlen])
