import importlib
import random

import pytest

from communications.native_protocol import (
    NativeV2StreamDecoder, create_v2_stream_decoder,
    native_protocol_available,
)
from communications.protocol_v2 import (
    MessageType, V2Frame, V2StreamDecoder, encode_v2_frame,
)


def _feed_in_chunks(decoder, wire: bytes, chunk_sizes: list[int]):
    frames = []
    offset = 0
    for size in chunk_sizes:
        frames.extend(decoder.feed(wire[offset:offset + size]))
        offset += size
    frames.extend(decoder.feed(wire[offset:]))
    return frames


def test_原生协议未安装时自动回退(monkeypatch):
    monkeypatch.setenv("MOTOR_HMI_NATIVE_PROTOCOL", "python")
    decoder = create_v2_stream_decoder()

    assert decoder.backend == "python"
    assert decoder.feed(encode_v2_frame(
        V2Frame(MessageType.HEARTBEAT, sequence=7)))[0].sequence == 7


def test_自动模式优先原生且保留回退(monkeypatch):
    monkeypatch.delenv("MOTOR_HMI_NATIVE_PROTOCOL", raising=False)
    decoder = create_v2_stream_decoder()

    expected = "cpp" if native_protocol_available() else "python"
    assert decoder.backend == expected


def test_强制原生模式会明确报告缺失(monkeypatch):
    monkeypatch.setenv("MOTOR_HMI_NATIVE_PROTOCOL", "native")
    if native_protocol_available():
        assert isinstance(create_v2_stream_decoder(), NativeV2StreamDecoder)
    else:
        with pytest.raises(RuntimeError, match=r"C\+\+ 协议核心"):
            create_v2_stream_decoder()


@pytest.mark.skipif(not native_protocol_available(), reason="尚未构建 C++ 协议核心")
def test_CRC标准向量由原生核心通过():
    native = importlib.import_module("motor_core_cpp")
    assert native.crc16_ccitt(b"123456789") == 0x29B1


@pytest.mark.skipif(not native_protocol_available(), reason="尚未构建 C++ 协议核心")
def test_原生与Python流解码在分包噪声损坏帧上完全一致():
    rng = random.Random(20260916)
    valid_frames = [
        V2Frame(
            MessageType(rng.randint(1, 7)), command=rng.randrange(256),
            payload=rng.randbytes(rng.randrange(0, 96)),
            address=rng.randrange(1, 16), sequence=index,
        )
        for index in range(120)
    ]
    wire_parts = [b"noise-prefix"]
    for index, frame in enumerate(valid_frames):
        encoded = encode_v2_frame(frame)
        wire_parts.append(encoded)
        if index % 11 == 0:
            broken = bytearray(encoded)
            broken[-3] ^= 0x5A
            wire_parts.append(bytes(broken))
        if index % 17 == 0:
            wire_parts.append(b"\x00\xA5garbage")
    wire = b"".join(wire_parts)
    chunks = []
    remaining = len(wire)
    while remaining:
        size = min(remaining, rng.randint(1, 47))
        chunks.append(size)
        remaining -= size

    python_decoder = V2StreamDecoder()
    native_decoder = NativeV2StreamDecoder()
    expected = _feed_in_chunks(python_decoder, wire, chunks)
    actual = _feed_in_chunks(native_decoder, wire, chunks)

    assert actual == expected == valid_frames
    assert native_decoder.error_count == python_decoder.error_count


@pytest.mark.skipif(not native_protocol_available(), reason="尚未构建 C++ 协议核心")
@pytest.mark.parametrize("split", range(1, 13))
def test_原生解码器保留每一种帧头与短帧切分(split):
    frames = [
        V2Frame(MessageType.HEARTBEAT, sequence=1),
        V2Frame(MessageType.TELEMETRY, 0xF1, b"x" * 22, sequence=2),
    ]
    wire = b"".join(map(encode_v2_frame, frames))
    decoder = NativeV2StreamDecoder()

    assert _feed_in_chunks(decoder, wire, [split]) == frames
    assert decoder.error_count == 0
