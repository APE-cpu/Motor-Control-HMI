/** 主机侧自测：gcc -std=c99 -Wall -Wextra -Werror ../v2_protocol_portable.c v2_protocol_selftest.c -I.. -o v2_selftest */
#include "v2_protocol_portable.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static uint8_t captured[V2_PROTO_MAX_FRAME];
static uint16_t captured_length;

static void capture_send(const uint8_t *data, uint16_t length)
{
    assert(length <= sizeof(captured));
    memcpy(captured, data, length);
    captured_length = length;
}

static int command_gate(uint8_t command, const uint8_t *payload,
                        uint16_t length, uint16_t *error_code,
                        const char **error_message)
{
    (void)payload;
    (void)length;
    if (command == V2_CMD_STOP) return 1;
    *error_code = 205u;
    *error_message = "power stage not ready";
    return 0;
}

static uint16_t make_request(uint8_t type, uint16_t sequence, uint8_t command,
                             const char *payload, uint8_t *out)
{
    uint16_t length = payload ? (uint16_t)strlen(payload) : 0u;
    uint16_t crc;
    uint16_t total = (uint16_t)(10u + length + 3u);
    out[0] = 0xA5u; out[1] = 0x5Au; out[2] = 2u; out[3] = 1u;
    out[4] = (uint8_t)sequence; out[5] = (uint8_t)(sequence >> 8);
    out[6] = type; out[7] = command;
    out[8] = (uint8_t)length; out[9] = (uint8_t)(length >> 8);
    if (length) memcpy(&out[10], payload, length);
    crc = v2_proto_crc16(&out[2], (uint16_t)(8u + length));
    out[10u + length] = (uint8_t)crc;
    out[11u + length] = (uint8_t)(crc >> 8);
    out[12u + length] = 0x7Eu;
    return total;
}

int main(void)
{
    static const uint8_t commands[] = {V2_CMD_START, V2_CMD_STOP};
    static const char * const fields[] = {"speed_actual", "fault_code"};
    V2ProtoConfig cfg;
    uint8_t request[V2_PROTO_MAX_FRAME];
    char nack_payload[V2_PROTO_MAX_PAYLOAD + 1u];
    uint16_t length;
    uint16_t nack_length;
    const V2ProtoStats *stats;

    assert(v2_proto_crc16((const uint8_t *)"123456789", 9u) == 0x29B1u);
    memset(&cfg, 0, sizeof(cfg));
    cfg.address = 1u;
    cfg.device_id = "SELFTEST-001";
    cfg.firmware_version = "test";
    cfg.hardware_version = "host";
    cfg.commands = commands;
    cfg.command_count = 2u;
    cfg.telemetry_fields = fields;
    cfg.telemetry_field_count = 2u;
    cfg.send = capture_send;
    cfg.on_command = command_gate;
    v2_proto_init(&cfg);

    length = make_request(V2_MSG_HELLO, 1u, 0u,
        "{\"client\":\"future\",\"protocol_min\":3,\"protocol_max\":3}", request);
    v2_proto_feed(request, length);
    assert(!v2_proto_session_ready());

    length = make_request(V2_MSG_HELLO, 1u, 0u,
        "{\"client\":\"selftest\",\"protocol_min\":2,\"protocol_max\":2}", request);
    v2_proto_feed(request, length);
    assert(v2_proto_session_ready());
    assert(captured_length > 13u && captured[6] == V2_MSG_CAPABILITIES);

    length = make_request(V2_MSG_HEARTBEAT, 2u, 0u, 0, request);
    v2_proto_feed(request, length);
    assert(captured[6] == V2_MSG_ACK && captured[4] == 2u);

    length = make_request(V2_MSG_COMMAND, 3u, V2_CMD_START, 0, request);
    v2_proto_feed(request, length);
    assert(captured[6] == V2_MSG_NACK && captured[7] == V2_CMD_START);
    nack_length = (uint16_t)captured[8] | ((uint16_t)captured[9] << 8);
    assert(nack_length <= V2_PROTO_MAX_PAYLOAD);
    memcpy(nack_payload, &captured[10], nack_length);
    nack_payload[nack_length] = '\0';
    assert(strstr(nack_payload, "205") != 0);

    length = make_request(V2_MSG_COMMAND, 4u, V2_CMD_STOP, 0, request);
    v2_proto_feed(request, length);
    assert(captured[6] == V2_MSG_ACK && captured[7] == V2_CMD_STOP);

    request[length - 2u] ^= 1u;
    v2_proto_feed(request, length);
    stats = v2_proto_get_stats();
    assert(stats->crc_or_frame_errors == 2u); /* 不兼容HELLO + 损坏CRC */
    assert(stats->commands_accepted == 1u);
    assert(stats->commands_rejected == 1u);
    puts("v2 protocol selftest passed");
    return 0;
}
