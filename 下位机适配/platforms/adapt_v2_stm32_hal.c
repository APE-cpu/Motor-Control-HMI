/** STM32 HAL v2接入模板。请按工程修改UART句柄与app_v2_command。 */
#include "main.h"
#include "v2_protocol_portable.h"

#define V2_RX_RING_SIZE 256u

extern UART_HandleTypeDef huart2;

/* 必须由电机应用层实现：先检查预充、母线、驱动故障和状态，再决定ACK/NACK。 */
extern int app_v2_command(uint8_t command, const uint8_t *payload,
                          uint16_t length, uint16_t *error_code,
                          const char **error_message);

static uint8_t s_rx_byte;
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

static void uart_send(const uint8_t *data, uint16_t length)
{
    /* 只会从主循环/通信任务调用。量产工程可替换为DMA发送队列。 */
    (void)HAL_UART_Transmit(&huart2, (uint8_t *)data, length, 100u);
}

void adapt_v2_stm32_init(void)
{
    V2ProtoConfig cfg;
    cfg.address = 1u;
    cfg.device_id = "STM32-MOTOR-001";
    cfg.firmware_version = "0.1.0-v2";
    cfg.hardware_version = "control-board-revA";
    cfg.commands = s_commands;
    cfg.command_count = (uint8_t)(sizeof(s_commands) / sizeof(s_commands[0]));
    cfg.telemetry_fields = s_fields;
    cfg.telemetry_field_count = (uint8_t)(sizeof(s_fields) / sizeof(s_fields[0]));
    cfg.send = uart_send;
    cfg.on_command = app_v2_command;
    v2_proto_init(&cfg);
    s_rx_head = s_rx_tail = 0u;
    s_rx_overflow = 0u;
    (void)HAL_UART_Receive_IT(&huart2, &s_rx_byte, 1u);
}

/** 从用户工程的HAL_UART_RxCpltCallback中调用，保持中断处理短小。 */
void adapt_v2_stm32_uart_rx_complete(UART_HandleTypeDef *huart)
{
    uint16_t next;
    if (huart != &huart2) return;
    next = (uint16_t)((s_rx_head + 1u) % V2_RX_RING_SIZE);
    if (next != s_rx_tail) {
        s_rx_ring[s_rx_head] = s_rx_byte;
        s_rx_head = next;
    } else {
        ++s_rx_overflow;
    }
    (void)HAL_UART_Receive_IT(&huart2, &s_rx_byte, 1u);
}

/** 在主循环或低优先级通信任务中高频调用。 */
void adapt_v2_stm32_poll(void)
{
    while (s_rx_tail != s_rx_head) {
        uint8_t byte = s_rx_ring[s_rx_tail];
        s_rx_tail = (uint16_t)((s_rx_tail + 1u) % V2_RX_RING_SIZE);
        v2_proto_feed_byte(byte);
    }
}

uint32_t adapt_v2_stm32_rx_overflow_count(void)
{
    return s_rx_overflow;
}
