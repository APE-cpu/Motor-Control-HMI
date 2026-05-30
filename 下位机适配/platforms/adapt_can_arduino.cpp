/**
 * adapt_can_arduino.cpp — Arduino MCP2515 (SPI) CAN 适配
 * 依赖库：MCP_CAN (https://github.com/coryjfowler/MCP_CAN_lib)
 */
#include <Arduino.h>
#include <mcp_can.h>
#include "can_protocol_portable.h"

static MCP_CAN CAN_BUS(10);  /* CS 引脚 10，按实际修改 */

static void my_can_send(uint32_t id, uint8_t *data, uint8_t len)
{
    CAN_BUS.sendMsgBuf(id, 0, len, data);
}

void adapt_can_arduino_init(void)
{
    CAN_BUS.begin(MCP_ANY, CAN_500KBPS, MCP_16MHZ);  /* 波特率/晶振按实际修改 */
    CAN_BUS.setMode(MCP_NORMAL);
    can_proto_init(my_can_send);
}
