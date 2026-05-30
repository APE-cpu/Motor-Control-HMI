/**
 * adapt_stm32_hal.c — STM32 HAL 适配示例
 * 把此文件加入工程，在 main.c 里 #include "protocol_portable.h" 后调用即可
 */
#include "protocol_portable.h"
#include "usart.h"  /* 包含 huart2 */

static void my_send(uint8_t byte)
{
    HAL_UART_Transmit(&huart2, &byte, 1, 10);
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

/* 在 main() 初始化阶段调用 */
void adapt_stm32_init(void)
{
    protocol_init(my_send, my_cmd);
}

/* 在串口接收中断/回调里调用 */
void adapt_stm32_rx(uint8_t byte)
{
    protocol_feed_byte(byte);
}
