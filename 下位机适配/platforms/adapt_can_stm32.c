/**
 * adapt_can_stm32.c — STM32 HAL CAN 适配
 */
#include "can_protocol_portable.h"
#include "can.h"  /* 包含 hcan1 */

static void my_can_send(uint32_t id, uint8_t *data, uint8_t len)
{
    CAN_TxHeaderTypeDef hdr;
    uint32_t mailbox;
    hdr.StdId = id;
    hdr.IDE   = CAN_ID_STD;
    hdr.RTR   = CAN_RTR_DATA;
    hdr.DLC   = len;
    hdr.TransmitGlobalTime = DISABLE;
    HAL_CAN_AddTxMessage(&hcan1, &hdr, data, &mailbox);
}

void adapt_can_stm32_init(void) { can_proto_init(my_can_send); }
