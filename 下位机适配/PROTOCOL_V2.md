# 上下位机通信协议 v2（迁移草案）

> 状态：上位机编解码、流解析、能力协商、ACK跟踪、心跳重启检测、`virtual-v2`及真实串口/TCP的`negotiated-v2`已实现；尚未完成下位机v2固件和无功率级实测，也未替换默认v1。

## 设计目标

- v1 与 v2 可并存，迁移期间不破坏现有设备。
- CRC16 检测传输破坏。
- 设备地址支持同一链路上的多设备识别。
- 16位消息序号把 ACK/NACK 与命令准确对应。
- 连接后先交换协议版本、固件身份和能力表。
- 未声明支持的命令不发送；版本不兼容不进入 READY。

## 帧格式

所有多字节整数采用小端序。

| 偏移 | 长度 | 字段 | 说明 |
|---:|---:|---|---|
| 0 | 2 | MAGIC | 固定 `A5 5A`，与 v1 的 `AA` 区分 |
| 2 | 1 | VERSION | 当前为 `02` |
| 3 | 1 | ADDRESS | 设备地址 `0..255` |
| 4 | 2 | SEQUENCE | 消息序号 `1..65535` |
| 6 | 1 | MESSAGE_TYPE | 消息类型，见下表 |
| 7 | 1 | COMMAND | 命令字；非命令消息可为0 |
| 8 | 2 | PAYLOAD_LEN | payload 长度，当前上限4096字节 |
| 10 | N | PAYLOAD | 消息内容 |
| 10+N | 2 | CRC16 | CRC-16/CCITT-FALSE，对 VERSION 至 PAYLOAD 计算 |
| 12+N | 1 | TAIL | 固定 `7E` |

CRC参数：`poly=0x1021`、`init=0xFFFF`、不反射、无最终异或；标准校验向量 `"123456789" → 0x29B1`。

## 消息类型

| 值 | 名称 | 方向/用途 |
|---:|---|---|
| 1 | COMMAND | 上位机下发命令 |
| 2 | ACK | 命令成功，复用原命令的 SEQUENCE 与 COMMAND |
| 3 | NACK | 命令拒绝，payload含错误码与原因 |
| 4 | HELLO | 上位机声明支持的协议范围 |
| 5 | CAPABILITIES | 下位机返回身份、版本、命令和遥测能力 |
| 6 | TELEMETRY | 版本化遥测 |
| 7 | HEARTBEAT | 会话心跳 |

NACK payload 当前采用 UTF-8 JSON：

```json
{"error_code":12,"message":"状态不允许启动"}
```

## 握手

```text
上位机                                  下位机
  │ HELLO(protocol_min=2,max=2)           │
  ├──────────────────────────────────────>│
  │                                       │ 校验版本与设备配置
  │ CAPABILITIES                           │
  │ device_id / firmware / hardware       │
  │ protocol_min/max / commands / fields  │
  │<──────────────────────────────────────┤
  │                                       │
  │ 版本交集存在 → READY                  │
  │ 无交集 → INCOMPATIBLE，禁止发命令     │
```

端口打开不等于设备就绪。只有握手成功、设备身份可识别、协议版本兼容后，通信会话才能进入 READY。

## 命令应答

1. 上位机为每条 COMMAND 分配非零 SEQUENCE。
2. 下位机处理成功后返回相同 SEQUENCE/COMMAND 的 ACK。
3. 状态不允许、参数越界或保护未复位时返回 NACK，不得静默忽略。
4. 上位机只接受当前待处理序号的应答；重复或迟到 ACK 不会匹配给其它命令。
5. 超时形成明确失败结果，启动/停机等危险命令的超时将由状态机处理。
6. **安全例外**：CMD_STOP（0x11）与 CMD_EMERGENCY_STOP（0x12）在会话尚未就绪时也允许发送，保命指令不依赖握手状态。

## 命令字一览

| 命令字 | 名称 | 方向 | 说明 |
|---:|---|---|---|
| 0x10 | CMD_START | 上位机→下位机 | 启动电机 |
| 0x11 | CMD_STOP | 上位机→下位机 | 正常停机（允许在未就绪时发送） |
| 0x12 | CMD_EMERGENCY_STOP | 上位机→下位机 | 紧急停机（允许在未就绪时发送） |
| 0x13 | CMD_RESET_FAULT | 上位机→下位机 | 故障复位（保护条件未清除时必须 NACK） |
| 0x20 | CMD_SET_PARAMS | 上位机→下位机 | 下发控制参数 |
| 0x21 | CMD_SET_SENSOR | 上位机→下位机 | 下发位置传感器配置 |

## 遥测帧格式（v2 专用）

v2 把遥测拆成**三个独立通道**，每个通道有独立的频率、用途和命令字。所有遥测帧的 `MESSAGE_TYPE=6 (TELEMETRY)`，载荷格式按 `COMMAND` 区分。

### 0xF0 常规遥测帧

| 介质 | 载荷字节数 | struct 格式 | 字段 |
|---|---|---|---|
| 串口/RS-485/TCP | 15 | `<hHhHhbBBBx` | speed_actual(h) speed_target(H) current_actual(h) angle_raw(H) angle_actual(h) temperature(b) sensor_quality(B) convergence(B) flags(B) padding(x) |
| CAN（v1 兼容） | 8 | `<hHhH` | speed_actual(h) speed_target(H) current_actual(h) angle_raw(H) |

字段标度：

| 字段 | 类型 | 物理量 | 换算 |
|---|---|---|---|
| speed_actual | int16 | 转速 rpm | 直接读 |
| speed_target | uint16 | 目标转速 rpm | 直接读 |
| current_actual | int16 | 电流 mA | `A = raw / 100` |
| angle_raw | uint16 | 传感器原始值 | 由 SENSOR_REGISTRY 定义 |
| angle_actual | int16 | 角度 0.01° | `deg = raw / 100` |
| temperature | int8 | 温度 °C | `°C = raw - 40` |
| sensor_quality | uint8 | 感器质量 0..255 | 255=最佳 |
| convergence | uint8 | 观测器收敛度 0..255 | 255=完全收敛 |
| flags | uint8 | 状态位 | bit0=低速警告, bit1=过流, bit2=驱动故障, bit3=急停 |
| padding | 1 byte | 填充 | 0 |

CAN 8 字节版本省略温度/质量/收敛度/flags，上位机沿用上一帧补全。

### 0xF1 高速遥测帧

高速通道用于电角度、Iq、相电流与施加电压，频率 200 Hz（UART/RS-485）或 1000 Hz（以太网 TCP，多样本批）。载荷可包含 1 个或多个样本，按 `payload_length` 判定单样本字节数：

| 载荷总长 | 单样本字节 | 内容 |
|---|---|---|
| 12 的倍数 | 12 | 精简帧：tick_ms + angle_raw + speed_rpm + iq + iqref |
| 16 的倍数 | 16 | 标准帧：精简 + Ia + Ib |
| 22 的倍数 | 22 | 扩展帧：标准 + Vd_raw + Vq_raw + Vbus_v |

**单样本布局**（小端序）：

| 偏移 | 字节数 | 类型 | 字段 | 换算 |
|---|---|---|---|---|
| 0 | 4 | uint32 | tick_ms | 下位机 HAL_GetTick 毫秒时间戳 |
| 4 | 2 | uint16 | angle_raw | `deg = raw × 360 / 65536` |
| 6 | 2 | int16 | speed_rpm | 直接读 |
| 8 | 2 | int16 | iq_raw | `A = raw × 0.000629` |
| 10 | 2 | int16 | iqref_raw | `A = raw × 0.000629`（同上） |
| 12 | 2 | int16 | ia_raw | `A = raw × 0.000629`（仅 16/22 字节帧） |
| 14 | 2 | int16 | ib_raw | `A = raw × 0.000629`（仅 16/22 字节帧） |
| 16 | 2 | int16 | vd_raw | MCSDK 内部 s16 码值，保留原始（仅 22 字节帧） |
| 18 | 2 | int16 | vq_raw | MCSDK 内部 s16 码值，保留原始（仅 22 字节帧） |
| 20 | 2 | uint16 | vbus_v | 母线电压，伏特整数（仅 22 字节帧） |

> `0.000629` 是 MCSDK 默认的 s16→A 标度，对应 ADC 满量程与运放增益组合。若下位机改动采样电阻或运放增益，需要同步修改 `comm_manager.py` 的解码常量。

### 0xF2 电流采样诊断帧

低速通道用于 ADC 注入采样诊断与 PWM duty 上报，典型频率 50 Hz。载荷长度为 21、29 或 31 字节，对应基本、带校准、带 VDDA 三档。

**21 字节基础布局**（小端序）：

| 偏移 | 字节数 | 类型 | 字段 | 换算 |
|---|---|---|---|---|
| 0 | 4 | uint32 | tick_ms | 下位机毫秒时间戳 |
| 4 | 2 | uint16 | adc1_raw | `V = raw × 3.3 / 32768` |
| 6 | 2 | uint16 | adc2_raw | `V = raw × 3.3 / 32768` |
| 8 | 2 | uint16 | offset_a | A 相零点，MCSDK 已翻倍：`zero_v = (raw / 2) × 3.3/32768` |
| 10 | 2 | uint16 | offset_b | B 相零点，同上 |
| 12 | 1 | uint8 | sector | SVPWM 扇区 0..5 |
| 13 | 2 | uint16 | duty_a | A 相占空比原始码 |
| 15 | 2 | uint16 | duty_b | B 相占空比始码 |
| 17 | 2 | uint16 | duty_c | C 相占空比原始码 |
| 19 | 2 | uint16 | sample_point | ADC 注入触发点 |

**29 字节扩展**：在 21 字节后追加 8 字节校准数据：

| 偏移 | 字节数 | 类型 | 字段 |
|---|---|---|---|
| 21 | 2 | uint16 | cal_adc1_min |
| 23 | 2 | uint16 | cal_adc1_max |
| 25 | 2 | uint16 | cal_adc2_min |
| 27 | 2 | uint16 | cal_adc2_max |

> `cal_adcN_pp = cal_adcN_max - cal_adcN_min`，上位机用于判断标定窗口是否覆盖实测电流摆幅。

**31 字节扩展**：在 29 字节后追加 2 字节 VDDA：

| 偏移 | 字节数 | 类型 | 字段 | 换算 |
|---|---|---|---|---|
| 29 | 2 | uint16 | vdda_mv | `V = raw / 1000`，0=固件尚未完成首次测量 |

**电流换算公式**（写死在 `comm_manager.py`）：

```
adc_volts_per_count = 3.3 / 32768
amps_per_mc_digit  = 3.3 / (65536 × 0.01 × 8)
adc1_delta_a = (offset_a - 2 × adc1_raw) × amps_per_mc_digit
adc2_delta_a = (offset_b - 2 × adc2_raw) × amps_per_mc_digit
```

其中 `0.01` 是采样电阻（Ω），`8` 是运放增益。**这两个常量必须与下位机硬件一致**，否则 A 相电流读数会全错。

> STM32F4 注入组 ADC 采用 12 位结果左对齐至 16 位，bit15 用作符号位，有效范围 0..32760；MCSDK 把结果翻倍后再做 offset 减法，因此 `offset_*` 字段会出现大于 32768 的值（如 32840），这是协议特性，不是溢出。

## 心跳与会话失效

- READY会话由上位机周期发送HEARTBEAT；心跳同样分配序号并要求设备ACK。
- 设备重启后握手状态会被清除。旧会话的下一次心跳将收到NACK，或在设备无响应时形成超时。
- 一旦确认会话失效，上位机立即退出READY、记录失效原因并清空全部待ACK命令。
- 被清理的命令会收到明确的“会话失效”结果；后续危险命令因会话不再READY而被拒绝。
- 若设备当时正在运行，上层运行状态机按通信丢失进入FAULT_LOCKED，不把重新打开端口或重新握手等同于自动复位。
- 当前`virtual-v2`和`negotiated-v2`心跳周期均为0.5秒、应答超时为1秒；它们是联调初值，需要根据控制板负载和链路延迟重新标定。

## 协议统计

`CommManager.protocol_status()`提供当前通信会话的累计统计：发送帧、接收帧、遥测帧、CRC/帧错误、ACK、NACK、超时、迟到/重复ACK、握手次数及会话重启/失效次数。通信页只读显示这些计数，用于实验后追溯链路质量；它们不是实时保护量。

## 当前实现

- `communications/protocol_v2.py`：CRC、帧编解码、ACK/NACK、串流拆包。
- `communications/protocol_session.py`：HELLO/CAPABILITIES、版本协商、能力检查、心跳、会话失效、待应答命令和超时。
- `tests/test_protocol_v2.py`：15项协议破坏、握手、心跳和应答测试。
- `communications/v2_virtual_device.py`：纯内存虚拟下位机，以虚拟时钟模拟应答、遥测与故障。
- `tests/test_v2_virtual_device.py`：9项握手、命令和故障场景端到端测试。
- `tests/test_comm_virtual_v2.py`：9项通信管理、状态机、设备重启与统计集成测试。
- `CommManager.connect_negotiated_v2()`：在真实串口/TCP字节流上执行HELLO/CAPABILITIES严格握手、异步命令ACK、遥测、心跳和会话失效处理。
- `tests/test_comm_negotiated_v2.py`：6项真实驱动边界测试，覆盖任意分包、命令/遥测、握手无响应、设备复位、底层短写、CAN限制和通信页入口。
- `下位机适配/v2_protocol_portable.h/.c`：无动态内存的下位机参考栈，包含CRC、字节流接收、HELLO版本检查、CAPABILITIES、命令ACK/NACK、心跳和JSON遥测发送。
- `platforms/adapt_v2_stm32_hal.c`与`adapt_v2_ti_c2000.c`：中断入环形缓冲、主循环解析的接入模板。
- `tests/v2_protocol_selftest.c`：可由目标工具链或主机C99编译器运行的CRC、握手、版本拒绝、ACK/NACK及坏帧自测。
- `V2_无功率级联调清单.md`：从固件自检到12项故障场景及低压功率级准入门槛。

### negotiated-v2传输边界

- RS-232/RS-485：直接承载完整v2字节流，串流解码器允许任意分包、粘包及噪声恢复。
- TCP：使用`sendall`保证整帧提交；接收仍按任意字节块进入同一串流解码器。
- 经典CAN：**暂不支持v2**。v2最小帧超过8字节，现有CAN驱动会截断；必须先定义包含帧序号、分片序号、总长度、超时清理和乱序策略的分片层。
- 真实链路只有收到兼容的CAPABILITIES才报告连接成功。超时、版本不兼容和错误设备均保持失败，不自动发送v1探测或命令。

虚拟设备可直接在测试或脚本中配置：

```python
device.response_delay_s = 0.5       # 所有后续响应延迟500ms
device.drop_next_response()         # 丢弃下一条响应
device.corrupt_next_response()      # 破坏下一条响应的CRC
device.nack_next_command(205, "母线未预充")
device.inject_fault(0x42, "栅极驱动故障")
device.reboot()                     # 清除握手并模拟设备重启
```

延迟由调用方传入的虚拟时间驱动，不使用真实`sleep`，因此场景可重复且测试执行速度稳定。

## 后续接入顺序

1. ~~编写 v2 虚拟下位机/故障注入节点。~~（已完成）
2. ~~在 `CommManager` 增加 `legacy-v1 / virtual-v2` 模式，不改变默认v1。~~（已完成）
3. ~~增加 `negotiated-v2` 真实串口/TCP链路，握手失败不自动降级。~~（上位机侧已完成）
4. ~~下位机C参考协议栈实现同一CRC和帧结构。~~（源码完成，尚待目标工具链编译和控制板验证）
5. 先在无功率级控制板上验证握手、ACK、NACK、重启和超时。
6. 通过后才允许真实设备配置选择v2。
