# 电机控制上位机软件介绍文档

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
│   ├── training_page.py          # 模型训练
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
│   ├── protocol.py               # 帧编解码
│   └── comm_manager.py           # 上层 Qt 信号封装
├── training/
│   └── trainer.py                # MLP/CNN/LSTM/Transformer/RF/SVM 训练器
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
- 支持 RS-232 / RS-485 / CAN 总线，三种方式可热切换
- 完整可配置：端口、波特率、数据位、停止位、校验位、超时
- 自定义帧协议：`HEAD | CMD | LEN | PAYLOAD | CHKSUM | TAIL`
- 命令字：`0x10 启动 / 0x11 停止 / 0x12 紧急停止 / 0x20 设置参数`
- 异常处理：断线重连、超时提示、错误日志

### 3.5 模型训练（本次升级核心）
**模型选择**：MLP、1D-CNN、LSTM、Transformer、随机森林、SVM 共 6 种
**训练超参数**：
- 学习率、批大小、训练轮数、验证集比例、权重衰减、随机种子
- 优化器：Adam / SGD / RMSprop / AdamW
- 损失函数：MSELoss / L1Loss / BCELoss / CrossEntropy
- 学习率调度：StepLR / CosineAnnealingLR / ReduceLROnPlateau / None
- 各模型独立的结构超参数面板（隐藏维度、层数、Dropout、卷积核、d_model、nhead、n_estimators、SVM C/核函数 等）

**训练流程**：数据采集 → 数据清洗（去重、归一化、NaN 过滤、范围过滤） → 训练（实时 Loss 曲线、可中止）→ ONNX 导出 → 边缘 AI 部署。

### 3.6 AI 集成
- **大模型分析（ai_page）**：调用 OpenAI 兼容接口，对实时遥测进行自然语言诊断
- **边缘 AI（edge_ai_page）**：在本地通过 ONNX Runtime 加载模型并实时推理，输出异常分数

### 3.7 操作记录
所有关键操作（启动 / 停止 / 紧急停止 / 参数下发 / 训练 / 导出 / 通信切换）
均落盘到 `logs/operation_log.txt`，可在“操作记录”页面回看。

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

## 六、扩展指南

- **新增控制算法**：在 `controllers/` 新建 `xxx_controller.py` 继承 `BaseController`；在 `pages/control_param_panels/panels.py` 增对应面板；在 `control_page._MODE_REGISTRY` 注册；在 `config.CONTROL_MODES_BY_MOTOR` 中按电机类型登记。
- **新增电机类型**：在 `config.MOTOR_TYPES` 追加，并在 `CONTROL_MODES_BY_MOTOR` 中映射可用控制方式。
- **新增位置传感器**：在 `config.POSITION_SENSORS` 追加即可，UI 自动出现勾选项。
- **新增模型**：在 `training/trainer.py` 实现 `nn.Module`；在 `pages/training_page.py` 增对应 `_ModelPanel`；在 `config.TRAIN_MODEL_TYPES` 与 `_MODEL_PANELS` 字典登记。
- **新增通信方式**：实现 `BaseComm`，在 `CommManager.connect` 中分支。

## 七、可扩展可选功能（已部分实现）

- ✅ 数据集 CSV 导入 / 导出
- ✅ 操作日志持久化
- ✅ ONNX 模型导出与本地推理
- ✅ AI 大模型在线分析
- ⏳ 报警阈值与告警弹窗（可后续接入）
- ⏳ 远程控制（基于已有协议层扩展）
