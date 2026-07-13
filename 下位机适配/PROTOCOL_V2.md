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
