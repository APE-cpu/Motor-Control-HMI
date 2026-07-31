# 电机控制上位机软件

## 一、软件概述

本软件是一套面向多类型电机的模块化上位机控制与监测平台，基于 Python + PySide6 构建，
覆盖 **永磁同步电机（PMSM）**、**双凸极电机（SRM）** 和 **直线电机** 的实时监控、
控制策略下发、数据采集、数字孪生仿真、参数辨识、矢量可视化、
AI 异常检测与边缘 AI 推理的全流程开发与调试需求。

软件强调**模块化**与**可扩展性**：每个功能（页面、控制算法、通信协议、AI 模块）
均独立成单独的 Python 文件，方便单独维护、测试与升级。

## 二、软件架构

```
上位机/
├── main.py                       # 主程序入口（UTF-8 强制、滚轮事件过滤、训练页开关）
├── main_window.py                # 主窗口（左导航 + ResponsiveStack 滚动容器）
├── runtime_paths.py              # PyInstaller 单文件路径处理（_MEIPASS / 可写路径）
├── config/                       # 全局配置与样式
│   ├── config.py                 # 常量、参数 dataclass、协议命令字、SENSOR_REGISTRY
│   └── style.qss                 # 深色商务风格表
├── core/
│   └── runtime_state.py          # 设备运行状态机（DISCONNECTED..FAULT_LOCKED）
├── pages/                        # 各页面（每个页面一个文件）
│   ├── monitor_page.py           # 监控
│   ├── control_page.py           # 电机控制
│   ├── vector_page.py            # 矢量可视化（iα-iβ 电流圆 / ψα-ψβ 磁链圆）
│   ├── power_flow_page.py        # 功率流（逆变器输入/铜损/电磁/机械/母线/制动）
│   ├── identify_page.py          # 电机参数辨识（B / Tc / J）
│   ├── communication_page.py     # 通信设置
│   ├── current_sampling_page.py  # 电流采样诊断（0xF2 ADC/PWM 波形）
│   ├── experiment_page.py        # 可追溯实验工作流
│   ├── ai_page.py                # 在线大模型分析（支持图片理解）
│   ├── edge_ai_page.py           # 离线/边缘 AI 推理
│   ├── training_page/            # 模型训练子包（监督学习 + DRL）
│   ├── operation_log_page.py     # 操作记录
│   ├── manual_page.py            # 内置使用说明书页
│   └── control_param_panels/     # 各控制方式的参数子面板
├── controllers/                  # 控制算法（每种算法一个文件）
│   ├── pi_controller.py          # 增量式 PID + 抗饱和
│   ├── openloop_controller.py
│   ├── mpc_controller.py         # 一阶系统 + 网格搜索 MPC
│   ├── sensorless_controller.py
│   ├── current_chopping_controller.py    # SRM CCC
│   ├── angle_position_controller.py      # SRM APC
│   └─ voltage_control_controller.py     # SRM 电压PWM
├── communications/               # 通信驱动
│   ├── base_comm.py              # 通信抽象基类
│   ├── serial_comm.py            # RS-232 / RS-485
│   ├── can_comm.py               # CAN（python-can 后端）
│   ├── zlgcan_comm.py            # 周立功 CAN——创芯 ControlCAN.dll（VCI_* 老接口）
│   ├── zlgcan_zcan_comm.py       # 周立功 CAN——致远原厂 zlgcan.dll（ZCAN_* 新接口）
│   ├── tcp_comm.py               # TCP/IP
│   ├── motor_sim.py              # 数字孪生 L1：PMSM dq 轴物理模型（虚拟下位机）
│   ├── protocol.py               # v1 帧编解码（0xAA/0x55 + 8 位累加和）
│   ├── protocol_v2.py            # v2 帧编解码（0xA5 0x5A + CRC16-CCITT-FALSE + 0x7E）
│   ├── protocol_session.py       # v2 握手、能力协商与命令应答跟踪
│   ├── v2_virtual_device.py      # 可脚本化 v2 虚拟下位机/故障注入节点
│   └── comm_manager.py           # 上层 Qt 信号封装（三通道遥测分发）
├── training/
│   ├── trainer.py                # MLP/CNN/LSTM/Transformer/RF/SVM 训练器
│   └── drl_trainer.py            # PPO/SAC/TD3 深度强化学习训练器
├─ edge_ai/
│   └── engine.py                 # ONNX Runtime 推理引擎
├── ai/
│   ├── ai_client.py              # OpenAI 兼容大模型客户端
│   └── rag.py                    # BM25 本地检索 + 中文字符二元组分词 + PDF OCR
├── experiments/                  # 可追溯实验持久化
│   ├── repository.py              # 目录式实验仓库（原子写 + fsync）
│   ├── session_manager.py        # 实验会话生命周期管理
│   ├── models.py / equipment.py / templates.py / recorder.py
│   ├── telemetry.py / report.py
├── widgets/                      # 自定义控件（雷达图、温度标签、趋势曲线等）
├── logs/
│   └── operation_logger.py       # 操作日志落盘
├── knowledge/                    # RAG 知识库（用户放置 md/txt/pdf）
├── zlgcan_x64/                   # 周立功驱动 DLL 及设备属性文件
├── 下位机适配/                   # 下位机协议移植包（STM32/C2000/Arduino/FPGA）
└── 波形记录/                     # 历史波形 CSV/PNG 落盘目录
```

> 数字孪生模型参数详见 [`数字孪生与电机参数.md`](数字孪生与电机参数.md)；v2 协议帧格式详见 [`下位机适配/PROTOCOL_V2.md`](下位机适配/PROTOCOL_V2.md)；上下行数据字典与字段语义（含 PWM 比较值/采样点/扇区/VDDA）详见 [`通信规约与数据字典.md`](通信规约与数据字典.md)。

## 三、主要功能特性

### 3.1 多电机类型与控制方式
- 支持三类电机：永磁同步电机（PMSM）、双凸极电机（SRM）、直线电机
- 控制方式按电机类型动态切换
  - **PMSM / 直线电机**：闭环 PI、开环、MPC、无位置传感器
  - **双凸极电机**：电流斩波控制（CCC）、角度位置控制（APC）、电压 PWM 控制、MPC
- 每种控制方式具备独立的参数面板，支持运行时动态调整与下发
- 电机级电流限幅同时作用于数字孪生速度环、真机参数帧和实验保护快照；真机仍必须具备独立硬件限流
- 控制页记录额定工作点温度，实际遥测温度集中在监控页展示

### 3.2 位置传感器多选
位置传感器列表**支持多选**，可同时勾选多种作为主用 / 兜底 / 估算方式。`config.SENSOR_REGISTRY` 维护每个传感器的元数据（sensor_id、bus、sample_rate_hz、can_id_default、allowed_modes、sensorless_method）：

| 类型 | id | 总线 | 采样率 | CAN ID | 说明 |
| --- | --- | --- | --- | --- | --- |
| 霍尔传感器 (Hall) | 0 | RS-485 | 6 kHz | 0x000 | 低成本、低分辨率，适合换相 |
| 增量式编码器 (QEP) | 1 | RS-485 | 20 kHz | 0x000 | 高分辨率，矢量控制常用 |
| 旋转变压器 (Resolver) | 2 | CAN 总线 | 10 kHz | 0x201 | 抗振动，工业/车规常用 |
| 无位置-滑模观测器 (SMO) | 3 | SPI（内部） | 10 kHz | 0x000 | 中高速估算 |
| 无位置-扩展卡尔曼 (EKF) | 4 | SPI（内部） | 10 kHz | 0x000 | 噪声鲁棒 |
| 无位置-模型参考自适应 (MRAS) | 5 | SPI（内部） | 10 kHz | 0x000 | 参数辨识友好 |
| 无位-高频注入 (HFI) | 6 | SPI（内部） | 4 kHz | 0x000 | 零低速估算 |

`allowed_modes=[]` 表示该传感器适配所有控制方式；`warn_modes` 列出兼容但不推荐的组合。每种传感器有独立的参数 dataclass（HallParams/QEPParams/ResolverParams/SMOParams/EKFParams/MRASParams/HFIParams），下位机收到 `CMD_SET_SENSOR(0x21)` 时按 sensor_id 切换内部观测器。

### 3.3 实时监控
- 实时显示：转速 / 电流 / 转矩 / 角度 / 温度
- 统计：转速、电流、转矩的最大值与最小值
- 趋势曲线：实际值与给定值同框对比，自动滚动最近 100 个点
- 定时刷新（默认 100 ms）
- 启动仿真只建立静止数据源，收到显式启动命令才旋转；停止仿真会复位模型、遥测、角度表和矢量图
- 只想观看波形时可使用“快速仿真演示”，自动建立仿真环境、执行仅限数字孪生的基础检查并启动电机；真机连接或故障锁定时禁用
- 主页面使用最小内容宽度与水平/垂直滚动容器，小屏不再强行压缩控件；监控页标题和运行按钮拆为两行
- 功率流页显示逆变器输入、铜损、电磁功率、机械功率、母线/制动支路的计算式、符号约定和模型边界
- 系统导航内置可重新载入的《使用说明书》，覆盖仿真、真机、预检、实验归档和异常处理流程

### 3.4 通信协议
- 支持 RS-232 / RS-485 / CAN 总线 / TCP/IP，四种方式可热切换
- CAN 总线支持三种后端驱动，在通信设置页"驱动接口"下拉框选择：
  - `zlgcan`：周立功/创芯 USBCAN，走 ControlCAN.dll（VCI_* 老接口）
  - `zlgcan-zcan`：致远原厂 zlgcan.dll（ZCAN_* 新接口）
  - 其余（slcan / pcan / kvaser 等）：走 python-can
- 完整可配置：端口、波特率、数据位、停止位、校验位、超时
- 配套 `下位机适配/` 移植包：C 语言协议栈 + STM32 / TI C2000 / Arduino / FPGA(Verilog) 平台适配层

#### v1 协议（默认，兼容老固件）
- 帧结构：`0xAA | CMD | LEN | PAYLOAD | 8位累加和 | 0x55`
- 校验：PAYLOAD 所有字节求和取低 8 位
- 命令字：`0x10 启动 / 0x11 停止 / 0x12 紧急停止 / 0x13 故障复位 / 0x20 设置参数 / 0x21 设置传感器`
- CAN 模式：8 字节裸数据 `<hHhH`（speed_actual/target/current/angle_raw），无帧头/校验/帧尾
- 串口遥测 payload 15 字节，struct 格式 `<hHhHhbBBBx`

#### v2 协议（实验工作流推荐）
- 帧结构：`0xA5 0x5A | version | address | sequence | msg_type | command | length(2B) | payload | CRC16-CCITT-FALSE | 0x7E`
- magic `0xA5 0x5A` 用于字节流对齐，`0x7E` 帧尾确认
- CRC16-CCITT-FALSE：`poly=0x1021, init=0xFFFF, 不反射, xor_out=0x0000`，校验向量 `"123456789" → 0x29B1`
- payload 上限 4096 字节
- 消息类型：COMMAND/ACK/NACK/HELLO/CAPABILITIES/TELEMETRY/HEARTBEAT
- 握手：HELLO/CAPABILITIES 协议版本协商 + 设备身份白名单（device_id / hardware_version / firmware_prefix），不匹配**立即拒绝且不降级到 v1**
- 心跳：0.5 s 周期、3 s ACK 超时；遥测持续时单独丢失心跳 ACK 不判定断链
- 三种连接模式：`connect()`（v1）/`connect_negotiated_v2()`（真实链路严格握手）/`connect_virtual_v2()`（内存虚拟下位机）
- **经典 CAN 不支持 v2**：v2 最小帧超过 8 字节，需先定义分片协议；CAN 上只能跑 v1 的 8 字节压缩遥测
- 完整帧格式与三种遥测帧字节布局见 [`下位机适配/PROTOCOL_V2.md`](下位机适配/PROTOCOL_V2.md)

#### 三通道遥测
上位机把遥测拆成三个独立通道，各自有独立的频率、格式和回调

| 命令 | 通道 | 典型频率 | 用途 |
|---|---|---|---|
| 0xF0 | `telemetryReceived` | 10 Hz | 速度/温度/故障等慢变量，UI 主曲线 |
| 0xF1 | `highRateTelemetryReceived` | 200 Hz（UART）/1 kHz（TCP 批量） | 电角度/Iq/Iqref/相电流/Vd/Vq/Vbus |
| 0xF2 | `currentSamplingDiagReceived` | 50 Hz | ADC 原始值/PWM duty/扇区/标定窗口 |

三通道独立时钟，时间轴不严格对齐。每帧的 `tick_ms` 是下位机 HAL_GetTick 毫秒时间戳，上位机额外存 `monotonic_s` 与墙上时间 ISO 字符串。

#### 运行状态机
所有危险运行意图通过 `core/runtime_state.py` 的状态机统一约束：

```
DISCONNECTED → CONNECTED → PRECHECK → READY → RUNNING → STOPPING → READY
                                            │
                                  FAULT_LOCKED ──reset_fault(人工)──> CONNECTED/DISCONNECTED
```

`FAULT_LOCKED` 跨重连保持，必须显式复位；RUNNING/STOPPING 期间断线立即 `FAULT_LOCKED`。

### 3.5 监督学习模型训练
**模型选择**：MLP、1D-CNN、LSTM、Transformer、随机森林、SVM 共 6 种

**训练超参数**：
- 学习率、批大小、训练轮数、验证集比例、权重衰减、随机种子
- 优化器：Adam / SGD / RMSprop / AdamW
- 损失函数：MSELoss / L1Loss / BCELoss / CrossEntropy
- 学习率调度：StepLR / CosineAnnealingLR / ReduceLROnPlateau / None
- 各模型独立的结构超参数面板（隐藏维度、层数、Dropout、卷积核、d_model、nhead、n_estimators、SVM C/核函数等）

**训练流程**：数据采集 → 数据清洗（去重、归一化、NaN 过滤、范围过滤）→ 训练（实时 Loss 曲线、可中止）→ ONNX 导出 → 边缘 AI 部署。

### 3.6 深度强化学习训练（DRL）
基于简化一阶电机模型的在线强化学习，支持三种算法：

| 算法 | 类型 | 特点 |
| --- | --- | --- |
| PPO | On-policy | 稳定、样本效率适中，含 GAE 优势估计 |
| SAC | Off-policy | 最大熵框架，探索性强 |
| TD3 | Off-policy | 双 Q 网络 + 目标策略平滑，减少过估计 |

**MPC 专家预热**：训练前可先用 MPC 控制器生成专家数据集，通过模仿学习对策略网络预热，显著加速 RL 收敛。支持独立生成专家数据集并保存/加载 CSV。

- 环境：`speed[k+1] = a·speed[k] + b·u[k]`，状态 3 维，动作连续 1 维
- 奖励：`-|speed - target| - 0.01·u²`
- DAgger 在线纠正：训练过程中周期性用 MPC 专家修正策略偏差

### 3.7 AI 集成
- **大模型分析**：调用 OpenAI 兼容接口，对实时遥测进行自然语言诊断
- **图片理解**：支持添加图片附件或一键抓取监控页波形截图，交给视觉大模型
  （如 qwen-vl 系列）判断电机运行状态与故障迹象
- **边缘 AI**：在本地通过 ONNX Runtime 加载模型并实时推理，输出异常分数，超阈值告警

### 3.8 操作记录
所有关键操作（启动 / 停止 / 紧急停止 / 参数下发 / 训练 / 导出 / 通信切换）
均落盘到 `logs/operation_log.txt`，可在"操作记录"页面回看。

### 3.9 数字孪生 L1（虚拟下位机）
`communications/motor_sim.py` 内置 PMSM dq 轴物理模型，完整参数表见 [`数字孪生与电机参数.md`](数字孪生与电机参数.md)。

- dq 电压方程 + 电磁转矩方程 + 机械方程 + 一阶热模型，欧拉法 0.5 ms 步长积分
- 每 0.1 s 对外吐一帧遥测，1 kHz 内部高速轨迹（每 2 步采 1 次）
- 内嵌转速/电流双闭环 PI：速度环 Kp=0.06/Ki=2.0（按 24V 小惯量电机整定），电流环 Kp=1.2/Ki=300（含交叉耦合前馈与抗饱和）
- L2 母线动力学：电源内阻、母线电容、带滞回的制动斩波器（v_brake_on=27V / v_brake_off=25.5V）、过压跳闸 v_ov_trip=30V
- 默认参数对齐**野火 42JSF840AS-1000-8 PMSM**（24V/4000rpm/4对极，Rs=0.59Ω, Ld=Lq=0.66mH, ψf=7.04mWb, J=1.85e-5 kg·m²）
- 充当"虚拟下位机"：仿真模式下控制页的启动 / 停止 / 急停 / 目标转速
  产生真实动态响应（阶跃、超调、滑行停机），齿槽转矩、库仑摩擦、铜损发热
- 拿到实际铭牌参数后替换 `PMSMParams` 字段即可，PI 参数真机套用前建议先降低 20% 再现场整定

### 3.10 矢量可视化（αβ 平面）
- **电流圆 iα-iβ** 与 **磁链圆 ψα-ψβ** 双图实时绘制，带余辉点云与当前矢量线
- 仿真模式直接取虚拟电机 1 kHz 高速轨迹；真机模式由 10 Hz 遥测按 id≈0 重构
- 几何解读：正圆 = 正常；圆度变差 / 偏心 = 不平衡、偏心、退磁等异常征兆

### 3.11 电机参数辨识
两点稳态 + 滑行实验，仅用转速与电流遥测辨识 **B（粘滞摩擦）/ Tc（库仑摩擦）/ J（转动惯量）**：
- 稳态：`Kt·iq = B·ω + Tc`，两个转速点解出 B、Tc（Kt = 1.5·p·ψf）
- 滑行：`J·dω/dt = −(B·ω + Tc)`，最小二乘拟合 J
- ψf 需由铭牌或反电动势实验提供；仿真模式下可用虚拟电机真值验证辨识精度
- 辨识结果可一键写回数字孪生参数，供矢量可视化页使用

## 四、控制方式与参数对照

| 控制方式 | 适用电机 | 参数 |
| --- | --- | --- |
| 闭环 PI 控制 | PMSM、直线 | Kp / Ki / Kd / 采样时间 |
| 开环控制 | 全部 | 幅值 / 频率 / 占空比 |
| 模型预测控制 (MPC) | 全部 | 预测时域 N / 控制时域 M / 权重 Q,R / u_min, u_max |
| 无位置传感器控制 | PMSM、直线 | 观测器增益 / 估算方法 / 启动频率 / 启动电流 |
| 电流斩波控制 (CCC) | 双凸极 | 电流上/下限 / 斩波频率 / 滞环带宽 |
| 角度位置控制 (APC) | 双凸极 | 开通角 / 关断角 / 提前角 / 限流值 |
| 电压 PWM 控制 | 双凸极 | 直流母线电压 / 占空比 / PWM 频率 / 电压限幅 |

## 五、技术栈

| 类别 | 选型 |
| --- | --- |
| GUI 框架 | PySide6 (Qt 6) |
| 绘图 | pyqtgraph |
| 串口 | pyserial |
| CAN | python-can / 周立功 ControlCAN.dll / 周立功 zlgcan.dll |
| 深度学习 | PyTorch |
| 经典 ML | scikit-learn |
| 推理 | ONNX Runtime |
| 数据 | NumPy / pandas |
| 知识库 | BM25 本地检索（自实现） + pypdf + rapidocr_onnxruntime（可选） |
| AI 客户端 | OpenAI 兼容 HTTP 接口 |
| 打包 | PyInstaller（单文件，含 `runtime_paths.py` 处理 `_MEIPASS`） |

> `requirements.txt` 为运行依赖，`requirements-train.txt` 额外含 torch 等训练依赖（体积较大）。打包精简版不带训练依赖，启动时 `import torch` 失败会自动关闭训练页。

## 六、安装与运行

```bash
# 安装运行依赖（Python 3.10+）
pip install -r requirements.txt

# 如需使用模型训练页，再装训练依赖（含 torch，体积较大）
pip install -r requirements-train.txt

# 启动
python main.py
```

首次启动默认进入监控页面，`CommManager` 自动注入模拟遥测，无需连接下位机即可调试 UI。

## 七、典型工作流

### 可追溯实验工作流

1. 实验管理 → 选择内置`78W PMSM 基础运行实验`或用户模板
2. 应用模板，核对实验目的、数据源、设备参数、安全边界和引导步骤
3. 新建实验后依次完成配置核对、接线确认、运行预检、实验动作、现象记录和安全停机
4. 实验运行中用快捷事件标记记录目标变化、负载、调参、振荡、异响或保护；标记会冻结当时遥测并显示在历史曲线时间轴
5. 每个步骤的确认、备注或可选跳过都会写入实验事件；未完成必做步骤或设备仍在运行时不能正常归档
6. 正常结束后编辑结构化实验结论，并从历史列表查看冻结模板、工作流进度、带事件线的曲线和统计
7. 点击“生成实验报告”，得到`report.md`、自包含`report.html`和`telemetry.svg`；整个实验目录可直接复制进Obsidian

### PMSM 调试
1. 通信设置 → 连接串口
2. 电机控制 → 选择 PMSM、勾选 QEP + Hall、控制方式选闭环 PI
3. 设置 Kp/Ki，目标转速 1500 rpm，点击启动
4. 切到监控页观察跟随效果，调参后"保存/应用参数"

### 双凸极电机低速重载
1. 控制页电机类型切到"双凸极电机"
2. 控制方式选"电流斩波控制 (CCC)"
3. 设置 i_upper=10 A、i_lower=8 A、斩波频率 10 kHz，启动电机

### 异常检测模型训练与部署
1. 模型训练页 → 分别在正常/警告/异常工况采集数据
2. 数据清洗
3. 选择 LSTM，隐藏维度 64、层数 2，AdamW + CosineAnnealingLR，训练 100 轮
4. Loss 收敛后"导出 ONNX"
5. 切到"边缘 AI"页加载模型 → 实时推理

### DRL 控制策略训练
1. 模型训练页切到 DRL 标签
2. 先点击"生成 MPC 专家数据集"（约 10000 步）进行预热
3. 选择算法（PPO / SAC / TD3），设置隐藏维度、学习率、训练步数
4. 开启"MPC 参考预热"，点击"开始训练"
5. 观察 Actor Loss / Critic Loss 曲线收敛后停止，导出策略模型

## 八、扩展指南

- **新增控制算法**：在 `controllers/` 新建 `xxx_controller.py` 继承 `BaseController`；在 `pages/control_param_panels/panels.py` 增对应面板；在 `control_page._MODE_REGISTRY` 注册；在 `config.CONTROL_MODES_BY_MOTOR` 中按电机类型登记。
- **新增电机类型**：在 `config.MOTOR_TYPES` 追加，并在 `CONTROL_MODES_BY_MOTOR` 中映射可用控制方式。
- **新增位置传感器**：在 `config.POSITION_SENSORS` 追加即可，UI 自动出现勾选项。
- **新增监督学习模型**：在 `training/trainer.py` 实现 `nn.Module`；在 `pages/training_page.py` 增对应 `_ModelPanel`；在 `config.TRAIN_MODEL_TYPES` 与 `_MODEL_PANELS` 字典登记。
- **新增通信方式**：实现 `BaseComm`，在 `CommManager.connect` 中分支。

## 九、常见问题

**Q: 启动时报缺少 PySide6 / pyqtgraph？**
先执行 `pip install -r requirements.txt`。曲线区会降级显示文字提示，不影响主流程。

**Q: 没有真实下位机能用吗？**
可以。在"电机控制"页点击"启动仿真"即可生成模拟遥测，验证 UI 与训练流程。

**Q: 切换电机类型后控制方式列表变了？**
设计行为。每种电机有适配的控制方式，例如 CCC/APC 仅对双凸极电机有意义。

**Q: 训练随机森林为何不能导出 ONNX？**
ONNX 导出仅支持 PyTorch 模型（MLP / CNN / LSTM / Transformer）。

**Q: 位置传感器为何允许多选？**
工业项目中常并联多个传感器互相校验或冗余兜底（如 QEP 主用 + Hall 兜底，低速时 SMO 切换到 HFI）。

**Q: 紧急停止与停止有何区别？**
"停止电机"发送正常停机指令，下位机按减速曲线停车；"紧急停止"立即切断输出并复位上位机控制器内部状态。

## 十、可扩展可选功能

- ✅ 数据集 CSV 导入 / 导出
- ✅ 操作日志持久化
- ✅ ONNX 模型导出与本地推理
- ✅ AI 大模型在线分析（含图片理解）
- ✅ DRL 策略训练（PPO / SAC / TD3 + MPC 专家预热）
- ✅ 数字孪生 L1 虚拟电机 + 参数辨识 + 矢量可视化
- ✅ 周立功 USBCAN 双后端驱动（ControlCAN.dll / zlgcan.dll）
- ✅ 下位机协议移植包（STM32 / C2000 / Arduino / FPGA）
- ⏳ 报警阈值与告警弹窗（可后续接入）
- ⏳ 远程控制（基于已有协议层扩展）

## 十一、更新日志

- **v1.8**：实验室对拖联调版——通信页遥测档位（省流/标准/**辨识**/自定义）与
  **F3 在线 ARX/RLS** 开关；监视页「在线辨识」曲线（a1/L/R）；F1/F2/F3 分频与
  TCP 队列保护、控制/ACK 优先；波形批量导出与趋势曲线批处理；运行态与故障
  遥测硬化；与 F407 固件 `v2_rls`（F3+RUN 起 16 kHz 被动辨识、约 10 Hz 上报）
  联调通过。说明：默认「标准」档 F3 关；选「辨识」或勾选 F3 后再 START，加速段即可学习。
- **v1.7**：实验室真机联调——RS-232/以太网TCP negotiated-v2通信强化、控制与
  ACK优先、心跳独立超时；真实遥测与200 Hz高速角度/Iq/Iqref/相电流通道、ADC
  注入采样与PWM诊断页、波形CSV归档；野火78 W PMSM真实温度和母线采样、
  4000 rpm额定上限、运行保护回读；速度/电流PI在线下发和本机参数方案管理；
  GLM-5.2及多模型AI档案、API配置持久化；真实设备实验档案和模板默认值完善
- **v1.6**：完整实验工作流——实验模板、不可变设备档案、运行状态机、
  预检与步骤确认、事件标记、历史遥测、结构化结论及 Markdown/HTML/SVG
  报告；通信协议 v2——设备身份与能力协商、CRC16、序号、ACK/NACK、
  心跳、严格设备白名单、虚拟下位机与故障注入、STM32/C2000 可移植 C 包；
  运行体验——电机电流限幅、温度展示、快速仿真演示、停止后模型与角度复位、
  小屏滚动布局、功率流计算式和内置使用说明书
- **v1.5**：训练页特征选择、自动标注与扫频采集；数字孪生故障检测 ONNX；
  边缘 AI 模型选择、六维雷达图与时间去抖；控制页机械/负载扰动；
  日志搜索、级别筛选和逐行着色
- **v1.1**：模型训练扩展（6 种监督模型 + DRL）、双凸极电机控制方式、位置传感器多选
- **v1.2**：周立功 USBCAN 支持——新增创芯 ControlCAN.dll（VCI_*）与致远原厂
  zlgcan.dll（ZCAN_*）两套 CAN 后端，修复 CAN 接收日志乱码；下位机协议移植包
- **v1.3**：数字孪生 L1 虚拟电机、电机参数辨识（B/Tc/J）、矢量可视化
  （电流圆/磁链圆）、AI 图片理解（波形截图诊断）、操作日志模块化
- **v1.4**：AI 能力升级——RAG 知识库增强（BM25 本地检索，项目文档 +
  knowledge/ 资料 + 历史实验报告自动入库）、流式输出、AI 实验报告生成
  （参数辨识/运行实验一键成文，Markdown 存档）；控制页增强——PI 转速/
  电流双环参数分离、MPC 替代环节选择、全部控制方式数学模型与参数说明、
  电机详情（额定/实测/描述持久化）、传感器详情与在线自检；工程化——
  pytest 测试套件（39 用例）、单入口打包、依赖拆分、训练页拆包、
  版本号与 git tag 联动校验
