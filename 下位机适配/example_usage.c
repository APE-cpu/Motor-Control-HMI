/**
 * @file example_usage.c
 * @brief 下位机使用示例 - 演示如何发送遥测数据和接收控制命令
 */

#include "protocol.h"
#include <stdio.h>
#include <string.h>

/* ============ 模拟的硬件接口（需根据实际MCU替换） ============ */

// 串口发送函数（需替换为实际的UART发送）
void uart_send(const uint8_t *data, uint16_t len)
{
    // 示例：STM32 HAL库
    // HAL_UART_Transmit(&huart1, (uint8_t *)data, len, 100);

    // 示例：裸机寄存器操作
    // for (uint16_t i = 0; i < len; i++) {
    //     while (!(USART1->SR & USART_SR_TXE));
    //     USART1->DR = data[i];
    // }

    printf("UART发送 %d 字节: ", len);
    for (uint16_t i = 0; i < len; i++) {
        printf("%02X ", data[i]);
    }
    printf("\n");
}

// CAN发送函数（需替换为实际的CAN发送）
void can_send(uint32_t can_id, const uint8_t *data, uint8_t len)
{
    // 示例：STM32 HAL库
    // CAN_TxHeaderTypeDef tx_header;
    // uint32_t tx_mailbox;
    // tx_header.StdId = can_id;
    // tx_header.IDE = CAN_ID_STD;
    // tx_header.RTR = CAN_RTR_DATA;
    // tx_header.DLC = len;
    // HAL_CAN_AddTxMessage(&hcan, &tx_header, (uint8_t *)data, &tx_mailbox);

    printf("CAN发送 ID=0x%03X, %d 字节: ", (unsigned int)can_id, len);
    for (uint8_t i = 0; i < len; i++) {
        printf("%02X ", data[i]);
    }
    printf("\n");
}

/* ============ 示例1：发送遥测数据（串口模式） ============ */
void example_send_telemetry_uart(void)
{
    // 准备遥测数据
    TelemetryPayload_t telem;
    telem.speed_actual = 1500;                          // 实际转速 1500 rpm
    telem.speed_target = 1500;                          // 目标转速 1500 rpm
    telem.current_actual = (int16_t)(5.2f * CURRENT_SCALE);  // 5.2A → 520 mA
    telem.angle_raw = 1234;                             // 传感器原始值（如QEP脉冲计数）
    telem.angle_actual = (int16_t)(120.5f * ANGLE_SCALE);    // 120.5° → 12050
    telem.temperature = (int8_t)(45.0f + TEMP_OFFSET);       // 45°C → 85
    telem.sensor_quality = 250;                         // 质量 250/255 ≈ 98%
    telem.convergence = 255;                            // 完全收敛
    telem.flags = 0x00;                                 // 无警告
    telem.padding = 0;

    // 编码为完整帧
    uint8_t tx_buf[32];
    uint16_t frame_len = protocol_encode_telemetry(&telem, tx_buf);

    // 发送
    uart_send(tx_buf, frame_len);

    // 预期输出：AA 01 0E [14字节payload] [校验和] 55
}

/* ============ 示例2：发送遥测数据（CAN模式） ============ */
void example_send_telemetry_can(void)
{
    // 准备CAN遥测数据（简化版，8字节）
    TelemetryPayloadCAN_t telem_can;
    telem_can.speed_actual = 1500;
    telem_can.speed_target = 1500;
    telem_can.current_actual = (int16_t)(5.2f * CURRENT_SCALE);
    telem_can.angle_raw = 1234;

    // 编码（无帧头/校验/帧尾）
    uint8_t tx_buf[8];
    uint16_t len = protocol_encode_telemetry_can(&telem_can, tx_buf);

    // 发送到CAN总线（ID可配置，默认0x100）
    can_send(0x100, tx_buf, len);
}

/* ============ 示例3：接收并解析上位机命令（串口模式） ============ */
void example_receive_command(const uint8_t *rx_data, uint16_t rx_len)
{
    uint8_t cmd;
    const uint8_t *payload;
    uint8_t payload_len;

    // 解码帧
    if (!protocol_decode_frame(rx_data, rx_len, &cmd, &payload, &payload_len)) {
        printf("帧解码失败：校验错误或格式错误\n");
        return;
    }

    // 根据命令字处理
    switch (cmd) {
        case CMD_START:
            printf("收到启动命令\n");
            // TODO: 启动电机
            break;

        case CMD_STOP:
            printf("收到停止命令\n");
            // TODO: 停止电机
            break;

        case CMD_EMERGENCY_STOP:
            printf("收到紧急停止命令\n");
            // TODO: 紧急停止（立即关闭PWM）
            break;

        case CMD_SET_PARAMS:
            printf("收到参数设置命令，payload长度=%d\n", payload_len);
            // TODO: 解析参数并应用
            // 参数格式需与上位机协商（如：控制模式、PID参数等）
            break;

        case CMD_SET_SENSOR:
            printf("收到传感器配置命令\n");
            // TODO: 配置位置传感器
            break;

        default:
            printf("未知命令字: 0x%02X\n", cmd);
            break;
    }
}

/* ============ 示例4：周期性发送遥测（定时器中断） ============ */
// 在定时器中断或主循环中周期性调用（如10ms一次）
void timer_callback_send_telemetry(void)
{
    static uint32_t counter = 0;
    counter++;

    // 从电机控制模块获取实时数据（示例）
    float speed_rpm = 1500.0f;      // 从速度环获取
    float current_a = 5.2f;         // 从电流采样获取
    float angle_deg = 120.5f;       // 从位置传感器获取
    float temp_c = 45.0f;           // 从温度传感器获取

    // 填充遥测数据
    TelemetryPayload_t telem;
    telem.speed_actual = (int16_t)speed_rpm;
    telem.speed_target = 1500;
    telem.current_actual = (int16_t)(current_a * CURRENT_SCALE);
    telem.angle_raw = counter % 2500;  // 示例：QEP计数
    telem.angle_actual = (int16_t)(angle_deg * ANGLE_SCALE);
    telem.temperature = (int8_t)(temp_c + TEMP_OFFSET);
    telem.sensor_quality = 255;
    telem.convergence = 255;
    telem.flags = (speed_rpm < 50.0f) ? 0x01 : 0x00;  // 低速警告
    telem.padding = 0;

    // 编码并发送
    uint8_t tx_buf[32];
    uint16_t len = protocol_encode_telemetry(&telem, tx_buf);
    uart_send(tx_buf, len);
}

/* ============ 示例5：串口接收状态机（处理分包） ============ */
#define RX_BUF_SIZE 128
static uint8_t rx_buffer[RX_BUF_SIZE];
static uint16_t rx_index = 0;

// 在串口接收中断中调用
void uart_rx_callback(uint8_t byte)
{
    // 查找帧头
    if (byte == FRAME_HEADER) {
        rx_index = 0;
        rx_buffer[rx_index++] = byte;
        return;
    }

    // 累积数据
    if (rx_index > 0 && rx_index < RX_BUF_SIZE) {
        rx_buffer[rx_index++] = byte;

        // 检查是否收到帧尾
        if (byte == FRAME_TAIL && rx_index >= 5) {
            // 尝试解析完整帧
            example_receive_command(rx_buffer, rx_index);
            rx_index = 0;  // 重置
        }
    } else {
        rx_index = 0;  // 溢出，重置
    }
}

/* ============ 主函数示例 ============ */
int main(void)
{
    printf("=== 下位机通信协议示例 ===\n\n");

    // 示例1：发送串口遥测
    printf("示例1：串口遥测数据\n");
    example_send_telemetry_uart();
    printf("\n");

    // 示例2：发送CAN遥测
    printf("示例2：CAN遥测数据\n");
    example_send_telemetry_can();
    printf("\n");

    // 示例3：解析上位机命令
    printf("示例3：解析启动命令\n");
    uint8_t cmd_start[] = {0xAA, 0x10, 0x00, 0x00, 0x55};  // 启动命令（无payload）
    example_receive_command(cmd_start, sizeof(cmd_start));
    printf("\n");

    printf("示例4：解析停止命令\n");
    uint8_t cmd_stop[] = {0xAA, 0x11, 0x00, 0x00, 0x55};   // 停止命令
    example_receive_command(cmd_stop, sizeof(cmd_stop));
    printf("\n");

    printf("示例5：解析紧急停止命令\n");
    uint8_t cmd_estop[] = {0xAA, 0x12, 0x00, 0x00, 0x55};  // 紧急停止
    example_receive_command(cmd_estop, sizeof(cmd_estop));
    printf("\n");

    return 0;
}
