/**
 * adapt_can_ti_c2000.c — TI C2000 DCAN 适配
 */
#include "can_protocol_portable.h"
#include "F28x_Project.h"

static void my_can_send(uint32_t id, uint8_t *data, uint8_t len)
{
    /* 使用邮箱 1 发送，需提前在初始化中配置为发送邮箱 */
    while (CANARegs.CANTRS.bit.TRS1);  /* 等待上次发送完成 */
    CANARegs.CANME.bit.ME1  = 0;       /* 暂时禁用邮箱 */
    CANARegs.CANMID1.bit.STDMSGID = id;
    CANARegs.CANMCF1.bit.DLC = len;
    /* 填充数据寄存器（C2000 CAN 数据寄存器为 32 位） */
    CANARegs.CANMDL1.all = ((uint32_t)data[3] << 24) | ((uint32_t)data[2] << 16)
                         | ((uint32_t)data[1] << 8)  | data[0];
    CANARegs.CANMDH1.all = ((uint32_t)data[7] << 24) | ((uint32_t)data[6] << 16)
                         | ((uint32_t)data[5] << 8)  | data[4];
    CANARegs.CANME.bit.ME1 = 1;
    CANARegs.CANTRS.bit.TRS1 = 1;     /* 触发发送 */
}

void adapt_can_c2000_init(void) { can_proto_init(my_can_send); }
