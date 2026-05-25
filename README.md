# 电机控制上位机软件 v1.1

## 一、软件概述

本软件是一套面向多类型电机的模块化上位机控制与监测平台，基于 Python + PySide6 构建，
覆盖 **永磁同步电机（PMSM）**、**双凸极电机（SRM）** 和 **直线电机** 的实时监控、
控制策略下发、数据采集、AI 异常检测与边缘 AI 推理的全流程开发与调试需求。

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
│   ├── communication_page.py     # 通信设置
│   ├── ai_page.py                # 在线大模型分析
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
│   ├── can_comm.py               # CAN
│   ├── tcp_comm.py               # TCP/IP
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
└── logs/                         # 操作日志
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
- 完整可配置：端口、波特率、数据位、停止位、校验位、超时
- 自定义帧协议：`HEAD | CMD | LEN | PAYLOAD | CHKSUM | TAIL`
- 命令字：`0x10 启动 / 0x11 停止 / 0x12 紧急停止 / 0x20 设置参数`
- 异常处理：断线重连、超时提示、错误日志

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
- **边缘 AI**：在本地通过 ONNX Runtime 加载模型并实时推理，输出异常分数，超阈值告警

### 3.8 操作记录
所有关键操作（启动 / 停止 / 紧急停止 / 参数下发 / 训练 / 导出 / 通信切换）
均落盘到 `logs/operation_log.txt`，可在"操作记录"页面回看。

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
- ✅ AI 大模型在线分析
- ✅ DRL 策略训练（PPO / SAC / TD3 + MPC 专家预热）
- ⏳ 报警阈值与告警弹窗（可后续接入）
- ⏳ 远程控制（基于已有协议层扩展）
