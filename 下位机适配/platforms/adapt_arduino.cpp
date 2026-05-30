/**
 * adapt_arduino.cpp — Arduino 适配示例
 */
#include <Arduino.h>
#include "protocol_portable.h"

static void my_send(uint8_t byte)
{
    Serial.write(byte);
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

void setup()
{
    Serial.begin(115200);
    protocol_init(my_send, my_cmd);
}

void loop()
{
    while (Serial.available())
        protocol_feed_byte(Serial.read());

    /* 每 100ms 发一次遥测 */
    static uint32_t last = 0;
    if (millis() - last >= 100) {
        last = millis();
        ProtoTelemetry_t t = {0};
        t.speed_actual = 1500;
        t.speed_target = 1500;
        t.temperature  = 65;  /* 25°C + 40 */
        t.sensor_quality = 255;
        t.convergence    = 255;
        protocol_send_telemetry(&t);
    }
}
