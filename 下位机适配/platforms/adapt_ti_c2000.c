/**
 * adapt_ti_c2000.c — TI C2000 (TMS320F28xxx) 适配示例
 */
#include "protocol_portable.h"
#include "F28x_Project.h"  /* TI C2000 头文件 */

static void my_send(uint8_t byte)
{
    while (!SciaRegs.SCICTL2.bit.TXRDY);
    SciaRegs.SCITXBUF.all = byte;
}

static void my_cmd(uint8_t cmd, const uint8_t *payload, uint8_t len)
{
    switch (cmd) {
        case CMD_START:          /* 启动电机 */ break;
        case CMD_STOP:           /* 停止电机 */ break;
        case CMD_EMERGENCY_STOP: /* 紧急停止 */ break;
        case CMD_SET_PARAMS:     /* 解析参数 */ break;
    }
}

void adapt_c2000_init(void)
{
    protocol_init(my_send, my_cmd);
}

/* 在 SCIA 接收中断里调用 */
__interrupt void sciaRxISR(void)
{
    protocol_feed_byte((uint8_t)SciaRegs.SCIRXBUF.all);
    PieCtrlRegs.PIEACK.all = PIEACK_GROUP9;
}
