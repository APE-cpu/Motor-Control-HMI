/**
 * can_protocol_portable.h
 * CAN 总线遥测协议（平台无关）
 *
 * 上位机 CAN 模式：8字节裸数据，无帧头/校验/帧尾
 * CAN ID 默认 0x100
 *
 * 使用方法：
 *   1. 实现发送函数 void my_can_send(uint32_t id, uint8_t *data, uint8_t len)
 *   2. 调用 can_proto_init(my_can_send) 注册
 *   3. 调用 can_proto_send_telemetry() 发送遥测
 *   4. 收到 CAN 报文时调用 can_proto_on_recv() 处理命令
 *      注意：上位机发命令时走串口协议，CAN 仅用于遥测上报
 */

#ifndef CAN_PROTOCOL_PORTABLE_H
#define CAN_PROTOCOL_PORTABLE_H

#include <stdint.h>

#define CAN_TELEM_ID  0x100   /* 遥测报文 ID，需与上位机一致 */

#pragma pack(push, 1)
typedef struct {
    int16_t  speed_actual;   /* rpm */
    uint16_t speed_target;   /* rpm */
    int16_t  current_actual; /* mA (A * 100) */
    uint16_t angle_raw;      /* 传感器原始值 */
} CanTelemetry_t;            /* 固定 8 字节 */
#pragma pack(pop)

typedef void (*can_send_fn)(uint32_t id, uint8_t *data, uint8_t len);

void can_proto_init(can_send_fn fn);
void can_proto_send_telemetry(const CanTelemetry_t *t);

#endif
