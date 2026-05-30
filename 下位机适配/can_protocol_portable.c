#include "can_protocol_portable.h"
#include <string.h>

static can_send_fn s_send = NULL;

void can_proto_init(can_send_fn fn) { s_send = fn; }

void can_proto_send_telemetry(const CanTelemetry_t *t)
{
    if (!s_send) return;
    uint8_t buf[8];
    memcpy(buf, t, sizeof(CanTelemetry_t));
    s_send(CAN_TELEM_ID, buf, 8);
}
