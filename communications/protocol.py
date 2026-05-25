"""数据帧编解码工具。

示例帧格式：
    [HEAD(1B)] [CMD(1B)] [LEN(1B)] [PAYLOAD(N)] [CHKSUM(1B)] [TAIL(1B)]
校验：所有 PAYLOAD 字节求和 & 0xFF。
"""
from typing import Optional, Tuple

from config.config import FRAME_HEADER, FRAME_TAIL


def encode_frame(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([FRAME_HEADER, cmd & 0xFF, len(payload) & 0xFF]) + payload
    chksum = sum(payload) & 0xFF
    return body + bytes([chksum, FRAME_TAIL])


def decode_frame(data: bytes) -> Optional[Tuple[int, bytes]]:
    """从 data 中解析一个完整帧；不完整或校验失败返回 None。"""
    if len(data) < 5:
        return None
    if data[0] != FRAME_HEADER or data[-1] != FRAME_TAIL:
        return None
    cmd = data[1]
    length = data[2]
    if len(data) != length + 5:
        return None
    payload = data[3:3 + length]
    chksum = data[3 + length]
    if chksum != (sum(payload) & 0xFF):
        return None
    return cmd, bytes(payload)
