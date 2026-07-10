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
├── main.py                       # 主程序入口
├── main_window.py                # 主窗口（左导航 + QStackedWidget）
├── config/                       # 全局配置与样式
│   ├── config.py                 # 常量、参数 dataclass、协议命令字
│   └── style.qss                 # 深色商务风格表
├── pages/                        # 各页面（每个页面一个文件）
│   ├── monitor_page.py           # 监控
│   ├── control_page.py           # 电机控制
│   ├── vector_page.py            # 矢量可视化（iα-iβ 电流圆 / ψα-ψβ 磁链圆）
│   ├── identify_page.py          # 电机参数辨识（B / Tc / J）
│   ├── communication_page.py     # 通信设置
│   ├── ai_page.py                # 在线大模型分析（支持图片理解）
│   ├── edge_ai_page.py           # 离线/边缘 AI 推理
│   ├── training_page.py          # 模型训练（监督学习 + DRL）
│   ├── operation_log_page.py     # 操作记录
│   └── control_param_panels/     # 各控制方式的参数子面板
├── controllers/                  # 控制算法（每种算法一个文件）
│   ├── pi_controller.py
│   ├── openloop_controller.py
│   ├── mpc_controller.py
│   ├── sensorless_controller.py
│   ├── current_chopping_controller.py    # SRM CCC
│   ├── angle_position_controller.py      # SRM APC
│   └── voltage_control_controller.py     # SRM 电压PWM
├── communications/               # 通信驱动
│   ├── serial_comm.py            # RS-232 / RS-485
│   ├── can_comm.py               # CAN（python-can 后端）
│   ├── zlgcan_comm.py            # 周立功 CAN——创芯 ControlCAN.dll（VCI_* 老接口）
│   ├── zlgcan_zcan_comm.py       # 周立功 CAN——致远原厂 zlgcan.dll（ZCAN_* 新接口）
│   ├── tcp_comm.py               # TCP/IP
│   ├── motor_sim.py              # 数字孪生 L1：PMSM dq 轴物理模型（虚拟下位机）
│   ├── protocol.py               # 帧编解码
│   └── comm_manager.py           # 上层 Qt 信号封装
├── training/
│   ├── trainer.py                # MLP/CNN/LSTM/Transformer/RF/SVM 训练器
│   └── drl_trainer.py            # PPO/SAC/TD3 深度强化学习训练器
├── edge_ai/
│   └── engine.py                 # ONNX Runtime 推理引擎
├── ai/
│   └── ai_client.py              # OpenAI 兼容大模型客户端
├── widgets/                      # 自定义控件
├── logs/                         # 操作日志（operation_logger 落盘）
├── zlgcan_x64/                   # 周立功驱动 DLL 及设备属性文件
└── 下位机适配/                   # 下位机协议移植包（STM32/C2000/Arduino/FPGA）
```

## 三、主要功能特性

### 3.1 多电机类型与控制方式
- 支持三类电机：永磁同步电机（PMSM）、双凸极电机（SRM）、直线电机
- 控制方式按电机类型动态切换
  - **PMSM / 直线电机**：闭环 PI、开环、MPC、无位置传感器
  - **双凸极电机**：电流斩波控制（CCC）、角度位置控制（APC）、电压 PWM 控制、MPC
- 每种控制方式具备独立的参数面板，支持运行时动态调整与下发

### 3.2 位置传感器多选
位置传感器列表**支持多选**，可同时勾选多种作为主用 / 兜底 / 估算方式：

| 类型 | 说明 |
| --- | --- |
| 霍尔传感器 (Hall) | 低成本、低分辨率，适合换相 |
| 增量式编码器 (QEP) | 高分辨率，矢量控制常用 |
| 旋转变压器 (Resolver) | 抗振动，工业/车规常用 |
| 无位置-滑模观测器 (SMO) | 中高速估算 |
| 无位置-扩展卡尔曼 (EKF) | 噪声鲁棒 |
| 无位置-模型参考自适应 (MRAS) | 参数辨识友好 |
| 无位置-高频注入 (HFI) | 零低速估算 |

### 3.3 实时监控
- 实时显示：转速 / 电流 / 转矩 / 角度 / 温度
- 统计：转速、电流、转矩的最大值与最小值
- 趋势曲线：实际值与给定值同框对比，自动滚动最近 100 个点
- 定时刷新（默认 100 ms）

### 3.4 通信协议
- 支持 RS-232 / RS-485 / CAN 总线 / TCP/IP，四种方式可热切换
- CAN 总线支持三种后端驱动，在通信设置页"驱动接口"下拉框选择：
  - `zlgcan`：周立功/创芯 USBCAN，走 ControlCAN.dll（VCI_* 老接口）
  - `zlgcan-zcan`：致远原厂 zlgcan.dll（ZCAN_* 新接口）
  - 其余（slcan / pcan / kvaser 等）：走 python-can
- 完整可配置：端口、波特率、数据位、停止位、校验位、超时
- 自定义帧协议：`HEAD | CMD | LEN | PAYLOAD | CHKSUM | TAIL`
- 命令字：`0x10 启动 / 0x11 停止 / 0x12 紧急停止 / 0x20 设置参数`
- 异常处理：断线重连、超时提示、错误日志
- 配套 `下位机适配/` 移植包：C 语言协议栈 + STM32 / TI C2000 / Arduino / FPGA(Verilog) 平台适配层

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
`communications/motor_sim.py` 内置 PMSM dq 轴物理模型：
- dq 电压方程 + 电磁转矩方程 + 机械方程 + 一阶热模型，欧拉法 0.5 ms 步长积分
- 内嵌转速/电流双闭环 PI，相当于下位机固件的控制环
- 充当"虚拟下位机"：仿真模式下控制页的启动 / 停止 / 急停 / 目标转速
  产生真实动态响应（阶跃、超调、滑行停机），含齿槽转矩、库仑摩擦、铜损发热
- 默认参数为 48V 小功率 PMSM 占位值，拿到实际铭牌参数后替换 `PMSMParams` 即可

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
| CAN | python-can |
| 深度学习 | PyTorch |
| 经典 ML | scikit-learn |
| 推理 | ONNX Runtime |
| 数据 | NumPy |

## 六、安装与运行

```bash
# 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 启动
python main.py
```

首次启动默认进入监控页面，`CommManager` 自动注入模拟遥测，无需连接下位机即可调试 UI。

## 七、典型工作流

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

- **v1.1**：模型训练扩展（6 种监督模型 + DRL）、双凸极电机控制方式、位置传感器多选
- **v1.2**：周立功 USBCAN 支持——新增创芯 ControlCAN.dll（VCI_*）与致远原厂
  zlgcan.dll（ZCAN_*）两套 CAN 后端，修复 CAN 接收日志乱码；下位机协议移植包
- **v1.3**：数字孪生 L1 虚拟电机、电机参数辨识（B/Tc/J）、矢量可视化
  （电流圆/磁链圆）、AI 图片理解（波形截图诊断）、操作日志模块化
