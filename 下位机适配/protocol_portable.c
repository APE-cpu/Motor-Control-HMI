#include "protocol_portable.h"
#include <string.h>

static proto_send_byte_fn  s_send = NULL;
static proto_on_command_fn s_cmd  = NULL;

/* 接收状态机 */
static uint8_t s_rx_buf[64];
static uint8_t s_rx_idx = 0;
static uint8_t s_in_frame = 0;

void protocol_init(proto_send_byte_fn send_fn, proto_on_command_fn cmd_fn)
{
    s_send = send_fn;
    s_cmd  = cmd_fn;
}

static uint8_t checksum(const uint8_t *p, uint8_t len)
{
    uint32_t s = 0;
    for (uint8_t i = 0; i < len; i++) s += p[i];
    return (uint8_t)(s & 0xFF);
}

static void send_byte(uint8_t b)
{
    if (s_send) s_send(b);
}

void protocol_send_telemetry(const ProtoTelemetry_t *t)
{
    const uint8_t plen = sizeof(ProtoTelemetry_t);
    send_byte(PROTO_HEADER);
    send_byte(CMD_TELEMETRY);
    send_byte(plen);
    const uint8_t *p = (const uint8_t *)t;
    for (uint8_t i = 0; i < plen; i++) send_byte(p[i]);
    send_byte(checksum(p, plen));
    send_byte(PROTO_TAIL);
}

void protocol_feed_byte(uint8_t byte)
{
    if (!s_in_frame) {
        if (byte == PROTO_HEADER) {
            s_rx_buf[0] = byte;
            s_rx_idx = 1;
            s_in_frame = 1;
        }
        return;
    }

    if (s_rx_idx >= sizeof(s_rx_buf)) { s_in_frame = 0; return; }
    s_rx_buf[s_rx_idx++] = byte;

    /* 至少收到3字节才知道长度 */
    if (s_rx_idx < 3) return;

    uint8_t plen  = s_rx_buf[2];
    uint8_t total = plen + 5;

    if (s_rx_idx < total) return;  /* 帧未收完 */

    s_in_frame = 0;
    if (s_rx_buf[total - 1] != PROTO_TAIL) return;
    if (s_rx_buf[3 + plen] != checksum(&s_rx_buf[3], plen)) return;
    if (s_cmd) s_cmd(s_rx_buf[1], &s_rx_buf[3], plen);
}
