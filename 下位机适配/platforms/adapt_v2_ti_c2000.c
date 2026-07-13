/** TI C2000 DriverLib/寄存器工程v2接入模板；示例使用SCIA寄存器。 */
#include "F28x_Project.h"
#include "v2_protocol_portable.h"

#define V2_RX_RING_SIZE 256u

extern int app_v2_command(uint8_t command, const uint8_t *payload,
                          uint16_t length, uint16_t *error_code,
                          const char **error_message);

static volatile uint16_t s_rx_head;
static volatile uint16_t s_rx_tail;
static volatile uint32_t s_rx_overflow;
static uint8_t s_rx_ring[V2_RX_RING_SIZE];

static const uint8_t s_commands[] = {
    V2_CMD_START, V2_CMD_STOP, V2_CMD_EMERGENCY_STOP,
    V2_CMD_RESET_FAULT, V2_CMD_SET_PARAMS, V2_CMD_SET_SENSOR
};
static const char * const s_fields[] = {
    "speed_actual", "speed_target", "current_actual", "current_target",
    "torque_actual", "torque_target", "angle_actual", "temperature",
    "vdc", "bus_state", "fault_code", "fault_text"
};

static void scia_send(const uint8_t *data, uint16_t length)
{
    uint16_t i;
    for (i = 0u; i < length; ++i) {
        while (SciaRegs.SCICTL2.bit.TXRDY == 0u) { }
        SciaRegs.SCITXBUF.all = data[i];
    }
}

void adapt_v2_c2000_init(void)
{
    V2ProtoConfig cfg;
    cfg.address = 1u;
    cfg.device_id = "C2000-MOTOR-001";
    cfg.firmware_version = "0.1.0-v2";
    cfg.hardware_version = "control-board-revA";
    cfg.commands = s_commands;
    cfg.command_count = (uint8_t)(sizeof(s_commands) / sizeof(s_commands[0]));
    cfg.telemetry_fields = s_fields;
    cfg.telemetry_field_count = (uint8_t)(sizeof(s_fields) / sizeof(s_fields[0]));
    cfg.send = scia_send;
    cfg.on_command = app_v2_command;
    v2_proto_init(&cfg);
    s_rx_head = s_rx_tail = 0u;
    s_rx_overflow = 0u;
}

/** 从SCIA RX ISR调用；ISR退出和PIE ACK仍由用户工程负责。 */
void adapt_v2_c2000_rx_isr_byte(void)
{
    uint16_t next = (uint16_t)((s_rx_head + 1u) % V2_RX_RING_SIZE);
    uint8_t byte = (uint8_t)(SciaRegs.SCIRXBUF.all & 0x00FFu);
    if (next != s_rx_tail) {
        s_rx_ring[s_rx_head] = byte;
        s_rx_head = next;
    } else {
        ++s_rx_overflow;
    }
}

void adapt_v2_c2000_poll(void)
{
    while (s_rx_tail != s_rx_head) {
        uint8_t byte = s_rx_ring[s_rx_tail];
        s_rx_tail = (uint16_t)((s_rx_tail + 1u) % V2_RX_RING_SIZE);
        v2_proto_feed_byte(byte);
    }
}

uint32_t adapt_v2_c2000_rx_overflow_count(void)
{
    return s_rx_overflow;
}
