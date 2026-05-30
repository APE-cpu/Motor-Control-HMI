# 下位机通信协议适配指南

## 概述

本文档说明如何让下位机适配上位机的通信协议，实现电机控制系统的数据交互。

---

## 一、协议规范

### 1.1 串口模式（RS-232/RS-485）

#### 帧格式
```
+--------+--------+--------+-----------+----------+--------+
| 帧头   | 命令字 | 长度   | 有效载荷  | 校验和   | 帧尾   |
| 0xAA   | 1字节  | 1字节  | N字节     | 1字节    | 0x55   |
+--------+--------+--------+-----------+----------+--------+
```

- **帧头**：固定 `0xAA`
- **命令字**：定义帧类型（见下表）
- **长度**：有效载荷字节数（不含帧头/命令/长度/校验/帧尾）
- **有效载荷**：实际数据内容
- **校验和**：有效载荷所有字节求和后取低8位（`sum & 0xFF`）
- **帧尾**：固定 `0x55`

#### 命令字定义

| 命令字 | 名称 | 方向 | 说明 |
|--------|------|------|------|
| `0x01` | 遥测数据 | 下位机→上位机 | 周期性上报电机状态 |
| `0x10` | 启动 | 上位机→下位机 | 启动电机运行 |
| `0x11` | 停止 | 上位机→下位机 | 正常停止电机 |
| `0x12` | 紧急停止 | 上位机→下位机 | 立即停止（安全保护） |
| `0x20` | 设置参数 | 上位机→下位机 | 下发控制参数 |
| `0x21` | 设置传感器 | 上位机→下位机 | 配置位置传感器 |

### 1.2 CAN总线模式

- **无帧头/校验/帧尾**，直接发送8字节裸数据
- **CAN ID**：默认 `0x100`（可配置）
- **数据格式**：见下文"遥测数据格式（CAN）"

---

## 二、遥测数据格式

### 2.1 串口模式（14字节有效载荷）

```c
#pragma pack(push, 1)
typedef struct {
    int16_t  speed_actual;      // 实际转速 (rpm)
    uint16_t speed_target;      // 目标转速 (rpm)
    int16_t  current_actual;    // 实际电流 (mA)
    uint16_t angle_raw;         // 传感器原始值
    int16_t  angle_actual;      // 实际角度 (0.01°)
    int8_t   temperature;       // 温度 (°C + 40)
    uint8_t  sensor_quality;    // 传感器质量 (0-255)
    uint8_t  convergence;       // 观测器收敛度 (0-255)
    uint8_t  flags;             // 标志位
    uint8_t  padding;           // 填充字节
} TelemetryPayload_t;  // 总共14字节
#pragma pack(pop)
```

**字段说明**：

| 字段 | 类型 | 单位/范围 | 说明 |
|------|------|-----------|------|
| `speed_actual` | int16 | rpm | 实际转速（有符号） |
| `speed_target` | uint16 | rpm | 目标转速 |
| `current_actual` | int16 | mA | 实际电流（需乘100，如5.2A→520） |
| `angle_raw` | uint16 | - | 传感器原始值（Hall步数/QEP脉冲等） |
| `angle_actual` | int16 | 0.01° | 实际角度（需乘100，如120.5°→12050） |
| `temperature` | int8 | °C+40 | 温度（加40偏置，如45°C→85） |
| `sensor_quality` | uint8 | 0-255 | 传感器数据质量（255=最佳） |
| `convergence` | uint8 | 0-255 | 无传感器观测器收敛度（255=完全收敛） |
| `flags` | uint8 | 位掩码 | bit0=低速警告，其他位保留 |
| `padding` | uint8 | - | 填充字节（保持结构对齐） |

**完整帧示例**（转速1500rpm，电流5.2A，角度120.5°，温度45°C）：
```
AA 01 0E F4 05 DC 05 08 02 D2 04 12 2F 55 FA FF 00 00 [校验和] 55
```

### 2.2 CAN模式（8字节裸数据）

```c
#pragma pack(push, 1)
typedef struct {
    int16_t  speed_actual;      // 实际转速 (rpm)
    uint16_t speed_target;      // 目标转速 (rpm)
    int16_t  current_actual;    // 实际电流 (mA)
    uint16_t angle_raw;         // 传感器原始值
} TelemetryPayloadCAN_t;  // 总共8字节
#pragma pack(pop)
```

**注意**：CAN模式为简化版，仅包含4个核心字段，无温度/质量/收敛度等扩展信息。

---

## 三、快速集成步骤

### 步骤1：添加协议文件到工程

将以下文件复制到下位机工程：
- `protocol.h` - 协议头文件
- `protocol.c` - 协议实现

### 步骤2：配置串口/CAN外设

**串口配置**（推荐）：
- 波特率：115200（可选9600/19200/38400/57600/230400）
- 数据位：8
- 停止位：1
- 校验位：无
- 流控：无

**CAN配置**：
- 波特率：500kbps（可选125k/250k/1M）
- 标准帧（11位ID）
- 默认ID：0x100

### 步骤3：周期性发送遥测数据

在定时器中断或主循环中（建议10-100ms周期）：

```c
void send_telemetry_periodic(void)
{
    // 1. 采集实时数据
    float speed = get_motor_speed();      // 从速度环获取
    float current = get_motor_current();  // 从电流采样获取
    float angle = get_rotor_angle();      // 从位置传感器获取
    float temp = get_temperature();       // 从温度传感器获取

    // 2. 填充遥测结构
    TelemetryPayload_t telem;
    telem.speed_actual = (int16_t)speed;
    telem.speed_target = (uint16_t)target_speed;
    telem.current_actual = (int16_t)(current * 100.0f);  // A → mA
    telem.angle_raw = get_sensor_raw_value();
    telem.angle_actual = (int16_t)(angle * 100.0f);      // ° → 0.01°
    telem.temperature = (int8_t)(temp + 40.0f);          // °C → int8
    telem.sensor_quality = 255;  // 根据实际传感器状态设置
    telem.convergence = 255;     // 无传感器模式下设置收敛度
    telem.flags = (speed < 50.0f) ? 0x01 : 0x00;  // 低速警告
    telem.padding = 0;

    // 3. 编码并发送
    uint8_t tx_buf[32];
    uint16_t len = protocol_encode_telemetry(&telem, tx_buf);
    uart_send(tx_buf, len);  // 替换为实际的UART发送函数
}
```

### 步骤4：接收并处理上位机命令

在串口接收中断或主循环中：

```c
void process_received_frame(const uint8_t *data, uint16_t len)
{
    uint8_t cmd;
    const uint8_t *payload;
    uint8_t payload_len;

    // 解码帧
    if (!protocol_decode_frame(data, len, &cmd, &payload, &payload_len)) {
        return;  // 校验失败
    }

    // 处理命令
    switch (cmd) {
        case CMD_START:
            motor_start();
            break;
        case CMD_STOP:
            motor_stop();
            break;
        case CMD_EMERGENCY_STOP:
            motor_emergency_stop();
            break;
        case CMD_SET_PARAMS:
            // 解析参数并应用（需根据实际参数格式实现）
            break;
        default:
            break;
    }
}
```

---

## 四、常见问题

### Q1: 如何处理串口接收的分包问题？

使用状态机累积字节，检测到帧尾后再解析：

```c
static uint8_t rx_buffer[128];
static uint16_t rx_index = 0;

void uart_rx_isr(uint8_t byte)
{
    if (byte == 0xAA) {  // 帧头
        rx_index = 0;
    }
    
    if (rx_index < sizeof(rx_buffer)) {
        rx_buffer[rx_index++] = byte;
    }
    
    if (byte == 0x55 && rx_index >= 5) {  // 帧尾
        process_received_frame(rx_buffer, rx_index);
        rx_index = 0;
    }
}
```

### Q2: 校验和计算错误怎么办？

确保只对**有效载荷**部分求和，不包含帧头/命令/长度/校验/帧尾：

```c
uint8_t checksum = 0;
for (int i = 0; i < payload_len; i++) {
    checksum += payload[i];
}
checksum &= 0xFF;  // 取低8位
```

### Q3: 字节序问题（大端/小端）

协议使用**小端序**（Little-Endian），STM32/ARM等MCU默认小端，可直接使用。如果MCU是大端，需手动转换：

```c
int16_t value = 0x1234;
uint8_t buf[2];
buf[0] = value & 0xFF;         // 低字节
buf[1] = (value >> 8) & 0xFF;  // 高字节
```

### Q4: CAN模式如何发送？

直接发送8字节裸数据，无需帧头/校验/帧尾：

```c
TelemetryPayloadCAN_t telem_can;
telem_can.speed_actual = 1500;
telem_can.speed_target = 1500;
telem_can.current_actual = 520;  // 5.2A
telem_can.angle_raw = 1234;

uint8_t tx_buf[8];
protocol_encode_telemetry_can(&telem_can, tx_buf);
can_send(0x100, tx_buf, 8);  // CAN ID = 0x100
```

---

## 五、测试验证

### 5.1 串口测试

使用串口调试助手发送启动命令：
```
AA 10 00 00 55
```
- `AA`：帧头
- `10`：启动命令
- `00`：payload长度为0
- `00`：校验和（payload为空，sum=0）
- `55`：帧尾

### 5.2 遥测数据验证

发送一帧遥测后，在上位机监控页面应看到：
- 转速曲线更新
- 电流/转矩显示
- 温度颜色提示（正常/高温）
- 角度实时变化

---

## 六、扩展建议

1. **参数下发格式**：`CMD_SET_PARAMS`的payload格式需与上位机协商，建议使用TLV（Type-Length-Value）结构
2. **错误码上报**：可在遥测帧的`flags`字段扩展更多状态位（过流/过压/堵转等）
3. **多电机支持**：CAN模式下可使用不同ID区分多个电机（如0x100/0x101/0x102）
4. **固件升级**：可扩展命令字支持Bootloader通信

---

## 七、参考资料

- 上位机源码：`AI工作文件夹/上位机/communications/`
- 协议定义：`AI工作文件夹/上位机/config/config.py`
- 示例代码：`example_usage.c`

---

**版本**：v1.0  
**更新日期**：2026-05-21
