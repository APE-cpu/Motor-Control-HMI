"""协议帧编解码测试：编码-解码往返、各类坏帧必须返回 None。"""
import pytest

from communications.protocol import decode_frame, encode_frame
from config.config import FRAME_HEADER, FRAME_TAIL


def test_编解码往返():
    frame = encode_frame(0x01, b"\x12\x34\x56")
    assert decode_frame(frame) == (0x01, b"\x12\x34\x56")


def test_空payload():
    frame = encode_frame(0x02)
    assert decode_frame(frame) == (0x02, b"")


def test_最大payload_255字节():
    payload = bytes(range(256))[:255]
    frame = encode_frame(0x03, payload)
    assert decode_frame(frame) == (0x03, payload)


def test_cmd超过一字节被截断():
    frame = encode_frame(0x1FF, b"")
    assert decode_frame(frame) == (0xFF, b"")


def test_校验和错误返回None():
    frame = bytearray(encode_frame(0x01, b"\x12\x34"))
    frame[-2] ^= 0xFF
    assert decode_frame(bytes(frame)) is None


def test_payload被篡改返回None():
    frame = bytearray(encode_frame(0x01, b"\x12\x34"))
    frame[3] ^= 0x01
    assert decode_frame(bytes(frame)) is None


def test_帧头错误返回None():
    frame = bytearray(encode_frame(0x01, b"\x12"))
    frame[0] = (FRAME_HEADER + 1) & 0xFF
    assert decode_frame(bytes(frame)) is None


def test_帧尾错误返回None():
    frame = bytearray(encode_frame(0x01, b"\x12"))
    frame[-1] = (FRAME_TAIL + 1) & 0xFF
    assert decode_frame(bytes(frame)) is None


@pytest.mark.parametrize("cut", [1, 2, 4])
def test_截断帧返回None(cut):
    frame = encode_frame(0x01, b"\x12\x34\x56")
    assert decode_frame(frame[:-cut]) is None


def test_过短数据返回None():
    assert decode_frame(b"") is None
    assert decode_frame(bytes([FRAME_HEADER])) is None


def test_长度字段与实际不符返回None():
    frame = bytearray(encode_frame(0x01, b"\x12\x34"))
    frame[2] = 5
    assert decode_frame(bytes(frame)) is None
