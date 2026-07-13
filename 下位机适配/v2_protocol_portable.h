/**
 * @file v2_protocol_portable.h
 * @brief Motor-Control-HMI v2 串口/TCP协议栈（无动态内存）
 *
 * 适用于STM32、TI C2000等裸机/RTOS工程。经典CAN不能直接使用本协议栈，
 * 因为v2帧大于8字节，需另行定义分片层。
 *
 * 重要：UART/SCI接收中断只应写入用户自己的环形缓冲；请在主循环或通信任务中
 * 调用v2_proto_feed_byte()/v2_proto_feed()，避免在中断中执行命令和发送应答。
 */
#ifndef V2_PROTOCOL_PORTABLE_H
#define V2_PROTOCOL_PORTABLE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define V2_PROTO_VERSION          2u
#define V2_PROTO_MAX_PAYLOAD      512u
#define V2_PROTO_MAX_FRAME        (V2_PROTO_MAX_PAYLOAD + 13u)

#define V2_MSG_COMMAND            1u
#define V2_MSG_ACK                2u
#define V2_MSG_NACK               3u
#define V2_MSG_HELLO              4u
#define V2_MSG_CAPABILITIES       5u
#define V2_MSG_TELEMETRY          6u
#define V2_MSG_HEARTBEAT          7u

#define V2_CMD_START              0x10u
#define V2_CMD_STOP               0x11u
#define V2_CMD_EMERGENCY_STOP     0x12u
#define V2_CMD_RESET_FAULT        0x13u
#define V2_CMD_SET_PARAMS         0x20u
#define V2_CMD_SET_SENSOR         0x30u

#define V2_ERR_NOT_HANDSHAKEN     100u
#define V2_ERR_UNSUPPORTED_CMD    101u
#define V2_ERR_COMMAND_REJECTED   102u

typedef void (*v2_proto_send_fn)(const uint8_t *data, uint16_t length);

/**
 * 命令处理回调。
 * 返回1：命令已被设备安全逻辑接受，协议栈发送ACK。
 * 返回0：拒绝命令，协议栈使用error_code/error_message发送NACK。
 * error_message指针必须在回调返回后仍有效（建议使用字符串常量）。
 */
typedef int (*v2_proto_command_fn)(
    uint8_t command,
    const uint8_t *payload,
    uint16_t payload_length,
    uint16_t *error_code,
    const char **error_message);

typedef struct {
    uint8_t address;
    const char *device_id;
    const char *firmware_version;
    const char *hardware_version;
    const uint8_t *commands;
    uint8_t command_count;
    const char * const *telemetry_fields;
    uint8_t telemetry_field_count;
    v2_proto_send_fn send;
    v2_proto_command_fn on_command;
} V2ProtoConfig;

typedef struct {
    uint32_t rx_frames;
    uint32_t tx_frames;
    uint32_t crc_or_frame_errors;
    uint32_t commands_accepted;
    uint32_t commands_rejected;
    uint32_t heartbeats;
    uint32_t address_mismatches;
} V2ProtoStats;

/** 初始化或重新初始化协议栈。config及其字符串/数组必须长期有效。 */
void v2_proto_init(const V2ProtoConfig *config);

/** 清除握手和接收状态；不会替用户停止电机或清除功率级故障。 */
void v2_proto_reset_session(void);

/** 在主循环/通信任务中输入接收字节。 */
void v2_proto_feed_byte(uint8_t byte);
void v2_proto_feed(const uint8_t *data, uint16_t length);

/** 会话是否已收到有效HELLO并发送CAPABILITIES。 */
int v2_proto_session_ready(void);

/**
 * 发送UTF-8 JSON遥测对象，例如 {"speed_actual":123.0,"fault_code":0}。
 * 返回1表示已交给send回调；未握手、超长或参数无效返回0。
 */
int v2_proto_send_telemetry_json(const char *json);

/** 读取累计统计，只读指针在下次init前有效。 */
const V2ProtoStats *v2_proto_get_stats(void);

/** CRC-16/CCITT-FALSE，便于控制板自检。 */
uint16_t v2_proto_crc16(const uint8_t *data, uint16_t length);

#ifdef __cplusplus
}
#endif

#endif
