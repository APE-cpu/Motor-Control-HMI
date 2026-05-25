# 电机控制上位机软件

模块化的 PySide6 上位机软件，用于实时监控和控制 PMSM / 双凸极电机 / 直线电机。

## 功能特性

- **左侧导航 + 三大功能页面**：监控、电机控制、通信设置
- **监控页面**：实时显示转速、电流、转矩、角度；统计最大/最小值；趋势曲线（含给定 vs 实际）
- **电机控制页面**：电机类型/参数配置、温度颜色提示、四种控制方式与各自参数面板（PI / 开环 / MPC / 无位置传感器）、启动/停止/紧急停止
- **通信设置页面**：RS-232 / RS-485 / CAN 总线，支持端口、波特率、数据位、停止位、校验位、超时配置；连接状态显示、数据收发测试、错误日志
- **模拟数据**：未连接下位机时也会注入模拟遥测，便于 UI 调试

## 项目结构

```
上位机/
├── main.py                       # 主程序入口
├── main_window.py                # 主窗口（导航 + 堆叠页面）
├── requirements.txt              # 依赖
├── README.md
├── config/
│   ├── config.py                 # 全局常量、参数 dataclass、协议命令字
│   └── style.qss                 # 深色商务风格表
├── pages/
│   ├── monitor_page.py           # 监控页面
│   ├── control_page.py           # 电机控制页面
│   ├── communication_page.py     # 通信设置页面
│   └── control_param_panels/     # 4 种控制方式的参数子面板
├── controllers/
│   ├── base_controller.py        # 控制器统一接口
│   ├── pi_controller.py          # 闭环 PI/PID
│   ├── openloop_controller.py    # 开环
│   ├── mpc_controller.py         # 模型预测控制
│   └── sensorless_controller.py  # 无位置传感器
├── communications/
│   ├── base_comm.py              # 通信驱动抽象
│   ├── serial_comm.py            # RS-232 / RS-485
│   ├── can_comm.py               # CAN 总线
│   ├── protocol.py               # 帧编解码（含校验）
│   └── comm_manager.py           # 上层管理器（线程 + Qt 信号）
└── widgets/
    ├── side_nav.py               # 侧边导航
    ├── temperature_label.py      # 带颜色提示的温度标签
    └── trend_curve.py            # 基于 pyqtgraph 的趋势曲线
```

## 安装

建议使用 Python 3.10+ 与虚拟环境：

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

启动后默认进入监控页面，由 `CommManager.start_simulation()` 注入模拟遥测，可立刻看到数据与曲线。

## 控制方式与参数对照

| 控制方式 | 参数 |
| --- | --- |
| 闭环 PI 控制 | Kp, Ki, Kd, 采样时间 |
| 开环控制 | 幅值, 频率, 占空比 |
| 模型预测控制 (MPC) | 预测时域 N, 控制时域 M, 权重 Q/R, 约束 u_min / u_max |
| 无位置传感器控制 | 观测器增益, 估算方法 (滑模/EKF/MRAS), 启动频率, 启动电流 |

切换控制方式时，参数面板自动切换（`QStackedWidget`）。

## 通信协议（示例）

```
[HEAD=0xAA] [CMD] [LEN] [PAYLOAD...] [CHKSUM] [TAIL=0x55]
```

- CHKSUM：PAYLOAD 所有字节求和 & 0xFF
- 命令字示例：`0x10 启动`、`0x11 停止`、`0x12 紧急停止`、`0x20 设置参数`

帧编解码见 `communications/protocol.py`，可按下位机协议扩展。

## 扩展指南

- 新增控制算法：在 `controllers/` 下新建 `xxx_controller.py`，继承 `BaseController`；
  在 `config.CONTROL_MODES` 与 `ControlPage._controllers / _panels` 中登记即可。
- 新增通信方式：实现 `BaseComm`，在 `CommManager.connect` 中分支。
- 新增电机类型：在 `config.MOTOR_TYPES` 中追加；如需差异化逻辑，可在 `ControlPage` 监听类型变化。

## 已知约束

- `pyqtgraph` 用于绘制趋势曲线；未安装时曲线区域会以文本提示降级。
- CAN 驱动依赖 `python-can`，且需根据硬件选择正确的 `interface`（PCAN / Kvaser / Vector 等）。
- 当前 `CommManager` 在轮询线程中始终注入模拟数据；接入真实下位机时，请把 `_make_simulated_frame` 替换为对 `recv` 解出的真实帧的解析。
