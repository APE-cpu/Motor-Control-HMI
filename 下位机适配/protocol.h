/**
 * @file protocol.h
 * @brief 上位机通信协议 - 下位机适配头文件
 * @note 与上位机 communications/protocol.py 对应
 */

#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <stdint.h>
#include <stdbool.h>

/* ============ 帧格式定义 ============ */
#define FRAME_HEADER    0xAA
#define FRAME_TAIL      0x55

/* ============ 命令字定义 ============ */
#define CMD_TELEMETRY       0x01    // 遥测数据上报
#define CMD_START           0x10    // 启动电机
#define CMD_STOP            0x11    // 停止电机
#define CMD_EMERGENCY_STOP  0x12    // 紧急停止
#define CMD_SET_PARAMS      0x20    // 设置参数
#define CMD_SET_SENSOR      0x21    // 设置传感器

/* ============ 缩放系数 ============ */
#define CURRENT_SCALE       100.0f  // A → mA
#define ANGLE_SCALE         100.0f  // ° → 0.01°
#define TEMP_OFFSET         40.0f   // °C → int8 (加偏置避免负数)

/* ============ 遥测数据结构（串口模式，14字节） ============ */
#pragma pack(push, 1)
typedef struct {
    int16_t  speed_actual;      // 实际转速 (rpm)
    uint16_t speed_target;      // 目标转速 (rpm)
    int16_t  current_actual;    // 实际电流 (mA)
    uint16_t angle_raw;         // 传感器原始值（Hall步数/QEP脉冲/角度等）
    int16_t  angle_actual;      // 实际角度 (0.01°)
    int8_t   temperature;       // 温度 (°C + 40)
    uint8_t  sensor_quality;    // 传感器质量 (0-255)
    uint8_t  convergence;       // 观测器收敛度 (0-255)
    uint8_t  flags;             // 标志位 (bit0: 低速警告)
    uint8_t  padding;           // 填充字节
} TelemetryPayload_t;
#pragma pack(pop)

/* ============ 遥测数据结构（CAN模式，8字节） ============ */
#pragma pack(push, 1)
typedef struct {
    int16_t  speed_actual;      // 实际转速 (rpm)
    uint16_t speed_target;      // 目标转速 (rpm)
    int16_t  current_actual;    // 实际电流 (mA)
    uint16_t angle_raw;         // 传感器原始值
} TelemetryPayloadCAN_t;
#pragma pack(pop)

/* ============ 完整帧结构（串口模式） ============ */
#define MAX_PAYLOAD_LEN 64
#pragma pack(push, 1)
typedef struct {
    uint8_t header;             // 帧头 0xAA
    uint8_t cmd;                // 命令字
    uint8_t length;             // 有效载荷长度
    uint8_t payload[MAX_PAYLOAD_LEN];  // 有效载荷
    uint8_t checksum;           // 校验和
    uint8_t tail;               // 帧尾 0x55
} Frame_t;
#pragma pack(pop)

/* ============ 函数声明 ============ */

/**
 * @brief 计算校验和（有效载荷所有字节求和 & 0xFF）
 * @param payload 有效载荷指针
 * @param len 有效载荷长度
 * @return 校验和
 */
uint8_t protocol_calc_checksum(const uint8_t *payload, uint8_t len);

/**
 * @brief 编码一帧数据（串口模式）
 * @param cmd 命令字
 * @param payload 有效载荷指针
 * @param payload_len 有效载荷长度
 * @param out_buf 输出缓冲区（至少 payload_len + 5 字节）
 * @return 编码后的总长度
 */
uint16_t protocol_encode_frame(uint8_t cmd, const uint8_t *payload, uint8_t payload_len, uint8_t *out_buf);

/**
 * @brief 解码一帧数据（串口模式）
 * @param data 接收到的数据
 * @param len 数据长度
 * @param out_cmd 输出命令字
 * @param out_payload 输出有效载荷指针（指向 data 内部）
 * @param out_payload_len 输出有效载荷长度
 * @return true=解码成功，false=校验失败或格式错误
 */
bool protocol_decode_frame(const uint8_t *data, uint16_t len,
                          uint8_t *out_cmd, const uint8_t **out_payload, uint8_t *out_payload_len);

/**
 * @brief 编码遥测数据帧（串口模式）
 * @param telem 遥测数据结构
 * @param out_buf 输出缓冲区（至少 19 字节）
 * @return 编码后的总长度
 */
uint16_t protocol_encode_telemetry(const TelemetryPayload_t *telem, uint8_t *out_buf);

/**
 * @brief 编码遥测数据（CAN模式，无帧头/校验/帧尾）
 * @param telem 遥测数据结构
 * @param out_buf 输出缓冲区（8字节）
 * @return 编码后的长度（固定8）
 */
uint16_t protocol_encode_telemetry_can(const TelemetryPayloadCAN_t *telem, uint8_t *out_buf);

#endif // PROTOCOL_H
