#include "v2_protocol_portable.h"

#include <stdio.h>
#include <string.h>

#define V2_MAGIC_0 0xA5u
#define V2_MAGIC_1 0x5Au
#define V2_TAIL    0x7Eu
#define V2_HEADER_LENGTH 10u

static V2ProtoConfig g_cfg;
static V2ProtoStats g_stats;
static uint8_t g_configured;
static uint8_t g_session_ready;
static uint8_t g_rx[V2_PROTO_MAX_FRAME];
static uint16_t g_rx_length;
static uint16_t g_rx_expected;
static uint8_t g_tx[V2_PROTO_MAX_FRAME];
static char g_json[V2_PROTO_MAX_PAYLOAD + 1u];

static void rx_reset(void)
{
    g_rx_length = 0u;
    g_rx_expected = 0u;
}

uint16_t v2_proto_crc16(const uint8_t *data, uint16_t length)
{
    uint16_t crc = 0xFFFFu;
    uint16_t i;
    uint8_t bit;
    if (data == 0) return crc;
    for (i = 0u; i < length; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (bit = 0u; bit < 8u; ++bit) {
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u)
                                  : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static int tx_frame(uint8_t type, uint16_t sequence, uint8_t command,
                    const uint8_t *payload, uint16_t payload_length)
{
    uint16_t crc;
    uint16_t total;
    if (!g_configured || g_cfg.send == 0 || payload_length > V2_PROTO_MAX_PAYLOAD)
        return 0;
    total = (uint16_t)(V2_HEADER_LENGTH + payload_length + 3u);
    g_tx[0] = V2_MAGIC_0;
    g_tx[1] = V2_MAGIC_1;
    g_tx[2] = V2_PROTO_VERSION;
    g_tx[3] = g_cfg.address;
    g_tx[4] = (uint8_t)(sequence & 0xFFu);
    g_tx[5] = (uint8_t)(sequence >> 8);
    g_tx[6] = type;
    g_tx[7] = command;
    g_tx[8] = (uint8_t)(payload_length & 0xFFu);
    g_tx[9] = (uint8_t)(payload_length >> 8);
    if (payload_length != 0u && payload != 0)
        memcpy(&g_tx[V2_HEADER_LENGTH], payload, payload_length);
    crc = v2_proto_crc16(&g_tx[2], (uint16_t)(8u + payload_length));
    g_tx[V2_HEADER_LENGTH + payload_length] = (uint8_t)(crc & 0xFFu);
    g_tx[V2_HEADER_LENGTH + payload_length + 1u] = (uint8_t)(crc >> 8);
    g_tx[total - 1u] = V2_TAIL;
    g_cfg.send(g_tx, total);
    ++g_stats.tx_frames;
    return 1;
}

static int append_raw(uint16_t *used, const char *text)
{
    size_t n;
    if (text == 0) text = "";
    n = strlen(text);
    if ((size_t)*used + n > V2_PROTO_MAX_PAYLOAD) return 0;
    memcpy(&g_json[*used], text, n);
    *used = (uint16_t)(*used + (uint16_t)n);
    g_json[*used] = '\0';
    return 1;
}

static int append_json_string(uint16_t *used, const char *text)
{
    const unsigned char *p = (const unsigned char *)(text ? text : "");
    char escaped[7];
    if (!append_raw(used, "\"")) return 0;
    while (*p != 0u) {
        if (*p == '"' || *p == '\\') {
            char pair[3] = {'\\', (char)*p, '\0'};
            if (!append_raw(used, pair)) return 0;
        } else if (*p < 0x20u) {
            (void)snprintf(escaped, sizeof(escaped), "\\u%04x", (unsigned)*p);
            if (!append_raw(used, escaped)) return 0;
        } else {
            char one[2] = {(char)*p, '\0'};
            if (!append_raw(used, one)) return 0;
        }
        ++p;
    }
    return append_raw(used, "\"");
}

static int send_capabilities(uint16_t sequence)
{
    uint16_t used = 0u;
    uint8_t i;
    char number[12];
    if (!append_raw(&used, "{\"device_id\":")) return 0;
    if (!append_json_string(&used, g_cfg.device_id)) return 0;
    if (!append_raw(&used, ",\"firmware_version\":")) return 0;
    if (!append_json_string(&used, g_cfg.firmware_version)) return 0;
    if (!append_raw(&used, ",\"hardware_version\":")) return 0;
    if (!append_json_string(&used, g_cfg.hardware_version)) return 0;
    if (!append_raw(&used,
        ",\"protocol_min\":2,\"protocol_max\":2,\"commands\":[")) return 0;
    for (i = 0u; i < g_cfg.command_count; ++i) {
        if (i != 0u && !append_raw(&used, ",")) return 0;
        (void)snprintf(number, sizeof(number), "%u", (unsigned)g_cfg.commands[i]);
        if (!append_raw(&used, number)) return 0;
    }
    if (!append_raw(&used, "],\"telemetry_fields\":[")) return 0;
    for (i = 0u; i < g_cfg.telemetry_field_count; ++i) {
        if (i != 0u && !append_raw(&used, ",")) return 0;
        if (!append_json_string(&used, g_cfg.telemetry_fields[i])) return 0;
    }
    (void)snprintf(number, sizeof(number), "%u", (unsigned)V2_PROTO_MAX_PAYLOAD);
    if (!append_raw(&used, "],\"max_payload\":")) return 0;
    if (!append_raw(&used, number)) return 0;
    if (!append_raw(&used, "}")) return 0;
    return tx_frame(V2_MSG_CAPABILITIES, sequence, 0u,
                    (const uint8_t *)g_json, used);
}

static int command_supported(uint8_t command)
{
    uint8_t i;
    for (i = 0u; i < g_cfg.command_count; ++i)
        if (g_cfg.commands[i] == command) return 1;
    return 0;
}

static int json_find_uint(const uint8_t *json, uint16_t length,
                          const char *key, uint16_t *value)
{
    uint16_t i;
    uint16_t key_length = (uint16_t)strlen(key);
    if (json == 0 || key == 0 || value == 0 || key_length == 0u) return 0;
    for (i = 0u; (uint32_t)i + key_length < length; ++i) {
        uint16_t pos;
        uint32_t parsed = 0u;
        int has_digit = 0;
        if (memcmp(&json[i], key, key_length) != 0) continue;
        pos = (uint16_t)(i + key_length);
        while (pos < length && (json[pos] == ' ' || json[pos] == '\t' ||
                                json[pos] == '\r' || json[pos] == '\n')) ++pos;
        if (pos >= length || json[pos++] != ':') continue;
        while (pos < length && (json[pos] == ' ' || json[pos] == '\t' ||
                                json[pos] == '\r' || json[pos] == '\n')) ++pos;
        while (pos < length && json[pos] >= '0' && json[pos] <= '9') {
            has_digit = 1;
            parsed = parsed * 10u + (uint32_t)(json[pos] - '0');
            if (parsed > 65535u) return 0;
            ++pos;
        }
        if (has_digit) {
            *value = (uint16_t)parsed;
            return 1;
        }
    }
    return 0;
}

static void send_nack(uint16_t sequence, uint8_t command,
                      uint16_t error_code, const char *message)
{
    uint16_t used = 0u;
    char number[12];
    (void)snprintf(number, sizeof(number), "%u", (unsigned)error_code);
    if (!append_raw(&used, "{\"error_code\":")) return;
    if (!append_raw(&used, number)) return;
    if (!append_raw(&used, ",\"message\":")) return;
    if (!append_json_string(&used, message)) return;
    if (!append_raw(&used, "}")) return;
    (void)tx_frame(V2_MSG_NACK, sequence, command,
                   (const uint8_t *)g_json, used);
}

static void handle_frame(void)
{
    uint8_t version = g_rx[2];
    uint8_t address = g_rx[3];
    uint16_t sequence = (uint16_t)g_rx[4] | ((uint16_t)g_rx[5] << 8);
    uint8_t type = g_rx[6];
    uint8_t command = g_rx[7];
    uint16_t payload_length = (uint16_t)g_rx[8] | ((uint16_t)g_rx[9] << 8);
    const uint8_t *payload = &g_rx[V2_HEADER_LENGTH];
    uint16_t received_crc = (uint16_t)g_rx[V2_HEADER_LENGTH + payload_length] |
        ((uint16_t)g_rx[V2_HEADER_LENGTH + payload_length + 1u] << 8);
    uint16_t actual_crc = v2_proto_crc16(&g_rx[2],
                                         (uint16_t)(8u + payload_length));
    uint16_t error_code;
    const char *error_message;
    int accepted;
    uint16_t protocol_min;
    uint16_t protocol_max;

    if (g_rx[g_rx_expected - 1u] != V2_TAIL || received_crc != actual_crc ||
            version != V2_PROTO_VERSION) {
        ++g_stats.crc_or_frame_errors;
        return;
    }
    ++g_stats.rx_frames;
    if (address != g_cfg.address) {
        ++g_stats.address_mismatches;
        return;
    }
    if (type == V2_MSG_HELLO) {
        g_session_ready = 0u;
        if (!json_find_uint(payload, payload_length, "\"protocol_min\"",
                            &protocol_min) ||
                !json_find_uint(payload, payload_length, "\"protocol_max\"",
                                &protocol_max) ||
                protocol_min > V2_PROTO_VERSION ||
                protocol_max < V2_PROTO_VERSION) {
            ++g_stats.crc_or_frame_errors;
            return;
        }
        if (send_capabilities(sequence)) g_session_ready = 1u;
        return;
    }
    if (type == V2_MSG_HEARTBEAT) {
        if (!g_session_ready) {
            send_nack(sequence, 0u, V2_ERR_NOT_HANDSHAKEN,
                      "protocol handshake required");
            return;
        }
        ++g_stats.heartbeats;
        (void)tx_frame(V2_MSG_ACK, sequence, 0u, 0, 0u);
        return;
    }
    if (type != V2_MSG_COMMAND) return;
    if (!g_session_ready) {
        ++g_stats.commands_rejected;
        send_nack(sequence, command, V2_ERR_NOT_HANDSHAKEN,
                  "protocol handshake required");
        return;
    }
    if (!command_supported(command)) {
        ++g_stats.commands_rejected;
        send_nack(sequence, command, V2_ERR_UNSUPPORTED_CMD,
                  "command not supported");
        return;
    }
    error_code = V2_ERR_COMMAND_REJECTED;
    error_message = "command rejected by device safety gate";
    accepted = g_cfg.on_command != 0 ?
        g_cfg.on_command(command, payload, payload_length,
                         &error_code, &error_message) : 0;
    if (accepted) {
        ++g_stats.commands_accepted;
        (void)tx_frame(V2_MSG_ACK, sequence, command, 0, 0u);
    } else {
        ++g_stats.commands_rejected;
        send_nack(sequence, command, error_code, error_message);
    }
}

void v2_proto_init(const V2ProtoConfig *config)
{
    memset(&g_cfg, 0, sizeof(g_cfg));
    memset(&g_stats, 0, sizeof(g_stats));
    g_configured = 0u;
    g_session_ready = 0u;
    rx_reset();
    if (config == 0 || config->send == 0 || config->device_id == 0 ||
            config->firmware_version == 0 || config->commands == 0 ||
            config->command_count == 0u) return;
    g_cfg = *config;
    g_configured = 1u;
}

void v2_proto_reset_session(void)
{
    g_session_ready = 0u;
    rx_reset();
}

int v2_proto_session_ready(void)
{
    return g_session_ready != 0u;
}

void v2_proto_feed_byte(uint8_t byte)
{
    if (!g_configured) return;
    if (g_rx_length == 0u) {
        if (byte == V2_MAGIC_0) g_rx[g_rx_length++] = byte;
        return;
    }
    if (g_rx_length == 1u) {
        if (byte == V2_MAGIC_1) {
            g_rx[g_rx_length++] = byte;
        } else if (byte != V2_MAGIC_0) {
            rx_reset();
        }
        return;
    }
    if (g_rx_length >= V2_PROTO_MAX_FRAME) {
        ++g_stats.crc_or_frame_errors;
        rx_reset();
        return;
    }
    g_rx[g_rx_length++] = byte;
    if (g_rx_length == V2_HEADER_LENGTH) {
        uint16_t payload_length = (uint16_t)g_rx[8] | ((uint16_t)g_rx[9] << 8);
        if (payload_length > V2_PROTO_MAX_PAYLOAD) {
            ++g_stats.crc_or_frame_errors;
            rx_reset();
            return;
        }
        g_rx_expected = (uint16_t)(V2_HEADER_LENGTH + payload_length + 3u);
    }
    if (g_rx_expected != 0u && g_rx_length == g_rx_expected) {
        handle_frame();
        rx_reset();
    }
}

void v2_proto_feed(const uint8_t *data, uint16_t length)
{
    uint16_t i;
    if (data == 0) return;
    for (i = 0u; i < length; ++i) v2_proto_feed_byte(data[i]);
}

int v2_proto_send_telemetry_json(const char *json)
{
    size_t length;
    if (!g_session_ready || json == 0) return 0;
    length = strlen(json);
    if (length > V2_PROTO_MAX_PAYLOAD) return 0;
    return tx_frame(V2_MSG_TELEMETRY, 0u, 0u,
                    (const uint8_t *)json, (uint16_t)length);
}

const V2ProtoStats *v2_proto_get_stats(void)
{
    return &g_stats;
}
