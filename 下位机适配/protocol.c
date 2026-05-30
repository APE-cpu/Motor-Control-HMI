/**
 * @file protocol.c
 * @brief 上位机通信协议 - 下位机适配实现
 */

#include "protocol.h"
#include <string.h>

/**
 * @brief 计算校验和
 */
uint8_t protocol_calc_checksum(const uint8_t *payload, uint8_t len)
{
    uint32_t sum = 0;
    for (uint8_t i = 0; i < len; i++) {
        sum += payload[i];
    }
    return (uint8_t)(sum & 0xFF);
}

/**
 * @brief 编码一帧数据（串口模式）
 */
uint16_t protocol_encode_frame(uint8_t cmd, const uint8_t *payload, uint8_t payload_len, uint8_t *out_buf)
{
    uint16_t idx = 0;

    out_buf[idx++] = FRAME_HEADER;
    out_buf[idx++] = cmd;
    out_buf[idx++] = payload_len;

    if (payload_len > 0 && payload != NULL) {
        memcpy(&out_buf[idx], payload, payload_len);
        idx += payload_len;
    }

    out_buf[idx++] = protocol_calc_checksum(payload, payload_len);
    out_buf[idx++] = FRAME_TAIL;

    return idx;
}

/**
 * @brief 解码一帧数据（串口模式）
 */
bool protocol_decode_frame(const uint8_t *data, uint16_t len,
                          uint8_t *out_cmd, const uint8_t **out_payload, uint8_t *out_payload_len)
{
    // 最小帧长度：帧头(1) + 命令(1) + 长度(1) + 校验(1) + 帧尾(1) = 5
    if (len < 5) {
        return false;
    }

    // 检查帧头和帧尾
    if (data[0] != FRAME_HEADER || data[len - 1] != FRAME_TAIL) {
        return false;
    }

    uint8_t cmd = data[1];
    uint8_t payload_len = data[2];

    // 检查长度一致性
    if (len != (uint16_t)(payload_len + 5)) {
        return false;
    }

    // 校验和验证
    const uint8_t *payload = &data[3];
    uint8_t checksum_recv = data[3 + payload_len];
    uint8_t checksum_calc = protocol_calc_checksum(payload, payload_len);

    if (checksum_recv != checksum_calc) {
        return false;
    }

    // 输出结果
    *out_cmd = cmd;
    *out_payload = payload;
    *out_payload_len = payload_len;

    return true;
}

/**
 * @brief 编码遥测数据帧（串口模式）
 */
uint16_t protocol_encode_telemetry(const TelemetryPayload_t *telem, uint8_t *out_buf)
{
    return protocol_encode_frame(CMD_TELEMETRY, (const uint8_t *)telem, sizeof(TelemetryPayload_t), out_buf);
}

/**
 * @brief 编码遥测数据（CAN模式）
 */
uint16_t protocol_encode_telemetry_can(const TelemetryPayloadCAN_t *telem, uint8_t *out_buf)
{
    memcpy(out_buf, telem, sizeof(TelemetryPayloadCAN_t));
    return sizeof(TelemetryPayloadCAN_t);
}
