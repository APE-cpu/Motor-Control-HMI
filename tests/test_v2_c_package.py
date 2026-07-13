import re
from pathlib import Path

from pycparser import c_parser

from communications.protocol_v2 import (
    MessageType, V2_MAX_PAYLOAD, V2_VERSION, crc16_ccitt,
)


ROOT = Path(__file__).parents[1]
ADAPTER = next(ROOT.glob("*/v2_protocol_portable.h")).parent


def _define(header: str, name: str) -> int:
    match = re.search(rf"^#define\s+{name}\s+(0x[0-9A-Fa-f]+|\d+)u?\s*$",
                      header, re.MULTILINE)
    assert match, f"未找到{name}"
    return int(match.group(1), 0)


def test_C协议常量与Python_v2保持一致():
    header = (ADAPTER / "v2_protocol_portable.h").read_text(encoding="utf-8")
    assert _define(header, "V2_PROTO_VERSION") == V2_VERSION
    assert _define(header, "V2_PROTO_MAX_PAYLOAD") <= V2_MAX_PAYLOAD
    for name, value in (
        ("V2_MSG_COMMAND", MessageType.COMMAND),
        ("V2_MSG_ACK", MessageType.ACK),
        ("V2_MSG_NACK", MessageType.NACK),
        ("V2_MSG_HELLO", MessageType.HELLO),
        ("V2_MSG_CAPABILITIES", MessageType.CAPABILITIES),
        ("V2_MSG_TELEMETRY", MessageType.TELEMETRY),
        ("V2_MSG_HEARTBEAT", MessageType.HEARTBEAT),
    ):
        assert _define(header, name) == int(value)
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_portable_C源码可由C99语法树解析():
    source = (ADAPTER / "v2_protocol_portable.c").read_text(encoding="utf-8")
    source = re.sub(r"^#.*$", "", source, flags=re.MULTILINE)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    preamble = r"""
typedef unsigned char uint8_t;
typedef unsigned short uint16_t;
typedef unsigned long uint32_t;
typedef unsigned long size_t;
typedef void (*v2_proto_send_fn)(const uint8_t *, uint16_t);
typedef int (*v2_proto_command_fn)(uint8_t,const uint8_t*,uint16_t,uint16_t*,const char**);
typedef struct { uint8_t address; const char *device_id; const char *firmware_version;
const char *hardware_version; const uint8_t *commands; uint8_t command_count;
const char * const *telemetry_fields; uint8_t telemetry_field_count;
v2_proto_send_fn send; v2_proto_command_fn on_command; } V2ProtoConfig;
typedef struct { uint32_t rx_frames; uint32_t tx_frames; uint32_t crc_or_frame_errors;
uint32_t commands_accepted; uint32_t commands_rejected; uint32_t heartbeats;
uint32_t address_mismatches; } V2ProtoStats;
void *memset(void*, int, size_t); void *memcpy(void*, const void*, size_t);
size_t strlen(const char*); int snprintf(char*, size_t, const char*, ...);
"""
    c_parser.CParser().parse(preamble + source)


def test_C自测与无功率级清单随移植包交付():
    selftest = ADAPTER / "tests" / "v2_protocol_selftest.c"
    checklist = ADAPTER / "V2_无功率级联调清单.md"
    assert selftest.exists()
    assert checklist.exists()
    text = checklist.read_text(encoding="utf-8")
    assert "不得从无功率级联调直接跳到500V平台" in text
    assert "V2-12" in text
