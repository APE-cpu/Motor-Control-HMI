/**
 * protocol_portable.h
 * 平台无关的上位机通信协议
 *
 * 使用方法：
 *   1. 实现一个字节发送函数，原型为 void my_send(uint8_t byte)
 *   2. 调用 protocol_init(my_send) 注册它
 *   3. 调用 protocol_send_telemetry() 发送遥测
 *   4. 每收到一个字节调用 protocol_feed_byte()，收到完整命令时回调 on_command
 */

#ifndef PROTOCOL_PORTABLE_H
#define PROTOCOL_PORTABLE_H

#include <stdint.h>
#include <stddef.h>

/* -------- 帧定义 -------- */
#define PROTO_HEADER        0xAA
#define PROTO_TAIL          0x55
#define CMD_TELEMETRY       0x01
#define CMD_START           0x10
#define CMD_STOP            0x11
#define CMD_EMERGENCY_STOP  0x12
#define CMD_SET_PARAMS      0x20

/* -------- 遥测结构（14字节，小端序） -------- */
#pragma pack(push, 1)
typedef struct {
    int16_t  speed_actual;   /* rpm */
    uint16_t speed_target;   /* rpm */
    int16_t  current_actual; /* mA，A*100 */
    uint16_t angle_raw;      /* 传感器原始值 */
    int16_t  angle_actual;   /* 0.01°，deg*100 */
    int8_t   temperature;    /* °C + 40 */
    uint8_t  sensor_quality; /* 0-255 */
    uint8_t  convergence;    /* 0-255 */
    uint8_t  flags;          /* bit0: 低速警告 */
    uint8_t  padding;
} ProtoTelemetry_t;
#pragma pack(pop)

/* -------- 回调类型 -------- */
typedef void (*proto_send_byte_fn)(uint8_t byte);
typedef void (*proto_on_command_fn)(uint8_t cmd, const uint8_t *payload, uint8_t len);

/* -------- API -------- */

/** 注册发送函数和命令回调，必须在使用前调用 */
void protocol_init(proto_send_byte_fn send_fn, proto_on_command_fn cmd_fn);

/** 发送一帧遥测数据 */
void protocol_send_telemetry(const ProtoTelemetry_t *t);

/** 每收到一个字节喂入此函数，内部自动组帧并触发命令回调 */
void protocol_feed_byte(uint8_t byte);

#endif /* PROTOCOL_PORTABLE_H */
