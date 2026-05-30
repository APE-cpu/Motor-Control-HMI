/**
 * adapt_stm32_can.c — STM32 HAL CAN 适配示例
 * CAN 模式使用简化协议：8字节裸数据，无帧头/校验/帧尾
 * CAN ID 默认 0x100（与上位机 comm_manager.py 一致）
 */
#include "main.h"
#include "can.h"   /* 包含 hcan1 */
#include <string.h>

#define CAN_TELEM_ID  0x100

#pragma pack(push, 1)
typedef struct {
    int16_t  speed_actual;   /* rpm */
    uint16_t speed_target;   /* rpm */
    int16_t  current_actual; /* mA (A * 100) */
    uint16_t angle_raw;      /* 传感器原始值 */
} CanTelemetry_t;            /* 固定 8 字节 */
#pragma pack(pop)

void can_send_telemetry(int16_t speed, uint16_t spd_tgt,
                        int16_t cur_ma, uint16_t ang_raw)
{
    CanTelemetry_t t;
    t.speed_actual   = speed;
    t.speed_target   = spd_tgt;
    t.current_actual = cur_ma;
    t.angle_raw      = ang_raw;

    CAN_TxHeaderTypeDef hdr;
    uint32_t mailbox;
    hdr.StdId = CAN_TELEM_ID;
    hdr.IDE   = CAN_ID_STD;
    hdr.RTR   = CAN_RTR_DATA;
    hdr.DLC   = sizeof(CanTelemetry_t);
    hdr.TransmitGlobalTime = DISABLE;

    HAL_CAN_AddTxMessage(&hcan1, &hdr, (uint8_t *)&t, &mailbox);
}
