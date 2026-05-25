"""全局配置常量。

包含：刷新周期、曲线缓冲长度、电机/控制方式/通信方式枚举、默认参数等。
"""
import struct
from dataclasses import dataclass, field
from typing import Dict, List


# ============ UI 刷新参数 ============
MONITOR_REFRESH_MS = 100         # 监控页面刷新周期
CURVE_BUFFER_SIZE = 100          # 曲线缓冲点数（最近 100 个点）
TEMP_NORMAL_THRESHOLD = 60.0     # 正常温度阈值（°C）
TEMP_HIGH_THRESHOLD = 85.0       # 高温阈值（°C）


# ============ 电机类型 ============
MOTOR_TYPES: List[str] = ["永磁同步电机(PMSM)", "双凸极电机", "直线电机"]


# ============ 控制方式 ============
PMSM_CONTROL_MODES: List[str] = [
    "闭环PI控制",
    "开环控制",
    "模型预测控制(MPC)",
]

# 双凸极电机专属控制方式
SRM_CONTROL_MODES: List[str] = [
    "电流斩波控制(CCC)",
    "角度位置控制(APC)",
    "电压PWM控制",
    "模型预测控制(MPC)",
]

# 按电机类型映射可用的控制方式
CONTROL_MODES_BY_MOTOR: Dict[str, List[str]] = {
    "永磁同步电机(PMSM)": PMSM_CONTROL_MODES,
    "双凸极电机": SRM_CONTROL_MODES,
    "直线电机": ["闭环PI控制", "开环控制", "模型预测控制(MPC)"],
}

# 兼容旧引用
CONTROL_MODES: List[str] = PMSM_CONTROL_MODES


# ============ 位置传感器 ============
POSITION_SENSORS: List[str] = [
    "霍尔传感器(Hall)",
    "增量式编码器(QEP)",
    "旋转变压器(Resolver)",
    "无位置传感器-滑模观测器(SMO)",
    "无位置传感器-扩展卡尔曼(EKF)",
    "无位置传感器-模型参考自适应(MRAS)",
    "无位置传感器-高频注入(HFI)",
]


# ============ 通信方式 ============
COMM_TYPES: List[str] = ["RS-232", "RS-485", "CAN总线", "以太网TCP"]
BAUD_RATES_SERIAL: List[int] = [9600, 19200, 38400, 57600, 115200, 230400]
BAUD_RATES_CAN: List[int] = [125_000, 250_000, 500_000, 1_000_000]
PARITY_OPTIONS: List[str] = ["无", "奇校验", "偶校验"]
STOP_BITS_OPTIONS: List[str] = ["1", "2"]


# ============ 控制器默认参数 ============
@dataclass
class PIParams:
    kp: float = 1.0
    ki: float = 0.1
    kd: float = 0.0
    sample_time: float = 0.001  # s


@dataclass
class OpenLoopParams:
    amplitude: float = 24.0   # 电压幅值 V
    frequency: float = 50.0   # 频率 Hz
    duty: float = 0.5         # 占空比


@dataclass
class MPCParams:
    prediction_horizon: int = 10
    control_horizon: int = 3
    weight_q: float = 1.0
    weight_r: float = 0.1
    u_max: float = 24.0
    u_min: float = -24.0


@dataclass
class SensorlessParams:
    observer_gain: float = 100.0
    method: str = "滑模观测器"   # 可选：滑模观测器 / 扩展卡尔曼 / 模型参考自适应
    start_freq: float = 5.0     # I/F 启动频率 Hz
    start_current: float = 2.0  # I/F 启动电流 A


@dataclass
class CurrentChoppingParams:
    current_upper: float = 8.0
    current_lower: float = 6.0
    chopping_frequency: float = 10_000.0
    hysteresis_band: float = 0.5


@dataclass
class AnglePositionParams:
    turn_on_angle: float = 5.0
    turn_off_angle: float = 25.0
    advance_angle: float = 0.0
    current_limit: float = 8.0


@dataclass
class VoltageControlParams:
    dc_bus_voltage: float = 48.0
    duty: float = 0.5
    pwm_frequency: float = 20_000.0
    voltage_limit: float = 48.0


@dataclass
class MotorConfig:
    motor_type: str = MOTOR_TYPES[0]
    model: str = "默认型号"
    pole_pairs: int = 4
    max_rpm: float = 3000.0


@dataclass
class AppConfig:
    motor: MotorConfig = field(default_factory=MotorConfig)
    pi: PIParams = field(default_factory=PIParams)
    openloop: OpenLoopParams = field(default_factory=OpenLoopParams)
    mpc: MPCParams = field(default_factory=MPCParams)
    sensorless: SensorlessParams = field(default_factory=SensorlessParams)
    current_chopping: CurrentChoppingParams = field(default_factory=CurrentChoppingParams)
    angle_position: AnglePositionParams = field(default_factory=AnglePositionParams)
    voltage_control: VoltageControlParams = field(default_factory=VoltageControlParams)
    active_mode: str = CONTROL_MODES[0]


SENSORLESS_METHODS: List[str] = ["滑模观测器", "扩展卡尔曼", "模型参考自适应", "高频注入"]


# ============ 模型训练 ============
TRAIN_MODEL_TYPES: List[str] = [
    "MLP (多层感知机)",
    "1D-CNN (一维卷积)",
    "LSTM (长短时记忆)",
    "Transformer",
    "随机森林 (Random Forest)",
    "支持向量机 (SVM)",
    "深度强化学习(DRL)",
]

TRAIN_OPTIMIZERS: List[str] = ["Adam", "SGD", "RMSprop", "AdamW"]
TRAIN_LOSSES: List[str] = ["MSELoss", "L1Loss", "BCELoss", "CrossEntropy"]
TRAIN_SCHEDULERS: List[str] = ["None", "StepLR", "CosineAnnealingLR", "ReduceLROnPlateau"]


# ============ 帧头/帧尾（示例协议） ============
FRAME_HEADER = 0xAA
FRAME_TAIL = 0x55


# ============ AI 大模型配置默认值 ============
AI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
AI_DEFAULT_MODEL = "gpt-4o"
AI_REQUEST_TIMEOUT = 30


# 命令字定义（与下位机协商后可在此扩展）
CMD_READ_STATUS = 0x01
CMD_TELEMETRY = CMD_READ_STATUS   # 上行遥测帧（语义命名）
CMD_START = 0x10
CMD_STOP = 0x11
CMD_EMERGENCY_STOP = 0x12
CMD_SET_PARAMS = 0x20
CMD_SET_SENSOR = 0x21        # 单独下发位置传感器配置


# ============ 上行遥测 payload 格式 ============
# 串口完整 14 字节 payload：
#   int16 speed_actual(rpm)、uint16 speed_target(rpm)
#   int16 current_actual(mA)、uint16 angle_raw(传感器自定义)
#   int16 angle_actual(0.01°)、int8 temperature(°C, 偏置 +40)
#   uint8 sensor_quality(0-255)、uint8 convergence(0-255)、uint8 flags、padding
TELEM_FMT = "<hHhHhbBBBx"
TELEM_LEN = struct.calcsize(TELEM_FMT)        # = 14

# CAN 8 字节裸 payload（无帧头/校验/帧尾，直接 4 个核心字段）：
#   int16 speed_actual、uint16 speed_target、int16 current_actual、uint16 angle_raw
TELEM_FMT_CAN = "<hHhH"
TELEM_LEN_CAN = struct.calcsize(TELEM_FMT_CAN)  # = 8

# 缩放系数
TELEM_CURRENT_SCALE = 100.0   # mA → A
TELEM_ANGLE_SCALE = 100.0     # 0.01° → °
TELEM_TEMP_OFFSET = 40.0      # int8 + 40 → °C
TELEM_TORQUE_FROM_CURRENT = 0.05   # current(A) × 系数 ≈ torque(Nm)，原型估算用


# ============ 位置传感器参数 ============
@dataclass
class HallParams:
    phase_sequence: str = "ABC"   # ABC / ACB
    pole_pairs: int = 4
    debounce_us: int = 10         # 边沿消抖时间


@dataclass
class QEPParams:
    lines_per_rev: int = 2500     # 每圈线数
    direction: int = 1            # +1/-1
    index_pulse: bool = True      # 是否使用 Z 相


@dataclass
class ResolverParams:
    pole_pairs: int = 1
    excitation_freq: float = 10_000.0   # 激励信号频率 Hz
    excitation_amp: float = 7.0         # 激励信号幅值 V
    tracking_bw: float = 500.0          # 跟踪环带宽 Hz


@dataclass
class SMOParams:
    gain_k: float = 100.0
    cutoff_freq: float = 200.0
    low_speed_threshold: float = 50.0   # 低于此转速估算不可用


@dataclass
class EKFParams:
    q_noise: float = 0.01
    r_noise: float = 0.1
    init_covariance: float = 1.0
    low_speed_threshold: float = 50.0


@dataclass
class MRASParams:
    adapt_gain: float = 50.0
    filter_tc: float = 0.002      # 滤波时间常数 s
    low_speed_threshold: float = 50.0


@dataclass
class HFIParams:
    inject_freq: float = 1_000.0  # 高频注入频率 Hz
    inject_amp: float = 5.0       # 注入幅值 V
    demod_gain: float = 200.0
    blend_speed: float = 100.0    # 切换到反电动势观测的速度阈值


# 传感器名称 → 元数据：sensor_id / 默认总线 / 采样率 / CAN 默认 ID /
#                      参数 dataclass / 允许的控制方式 / 不推荐的控制方式 /
#                      对应的无传感器估算方法
SENSOR_REGISTRY: Dict[str, dict] = {
    "霍尔传感器(Hall)": {
        "sensor_id": 0,
        "bus": "RS-485",
        "sample_rate_hz": 6_000,
        "can_id_default": 0x000,
        "params_cls": HallParams,
        "allowed_modes": ["闭环PI控制", "开环控制", "无位置传感器控制"],
        "warn_modes": ["模型预测控制(MPC)"],
        "sensorless_method": None,
    },
    "增量式编码器(QEP)": {
        "sensor_id": 1,
        "bus": "RS-485",
        "sample_rate_hz": 20_000,
        "can_id_default": 0x000,
        "params_cls": QEPParams,
        "allowed_modes": [],   # 空 = 全部允许
        "warn_modes": [],
        "sensorless_method": None,
    },
    "旋转变压器(Resolver)": {
        "sensor_id": 2,
        "bus": "CAN总线",
        "sample_rate_hz": 10_000,
        "can_id_default": 0x201,
        "params_cls": ResolverParams,
        "allowed_modes": [],
        "warn_modes": [],
        "sensorless_method": None,
    },
    "无位置传感器-滑模观测器(SMO)": {
        "sensor_id": 3,
        "bus": "SPI(内部)",
        "sample_rate_hz": 10_000,
        "can_id_default": 0x000,
        "params_cls": SMOParams,
        "allowed_modes": ["闭环PI控制", "开环控制", "无位置传感器控制"],
        "warn_modes": ["模型预测控制(MPC)"],
        "sensorless_method": "滑模观测器",
    },
    "无位置传感器-扩展卡尔曼(EKF)": {
        "sensor_id": 4,
        "bus": "SPI(内部)",
        "sample_rate_hz": 10_000,
        "can_id_default": 0x000,
        "params_cls": EKFParams,
        "allowed_modes": ["闭环PI控制", "开环控制", "无位置传感器控制"],
        "warn_modes": ["模型预测控制(MPC)"],
        "sensorless_method": "扩展卡尔曼",
    },
    "无位置传感器-模型参考自适应(MRAS)": {
        "sensor_id": 5,
        "bus": "SPI(内部)",
        "sample_rate_hz": 10_000,
        "can_id_default": 0x000,
        "params_cls": MRASParams,
        "allowed_modes": ["闭环PI控制", "开环控制", "无位置传感器控制"],
        "warn_modes": ["模型预测控制(MPC)"],
        "sensorless_method": "模型参考自适应",
    },
    "无位置传感器-高频注入(HFI)": {
        "sensor_id": 6,
        "bus": "SPI(内部)",
        "sample_rate_hz": 4_000,
        "can_id_default": 0x000,
        "params_cls": HFIParams,
        "allowed_modes": ["闭环PI控制", "无位置传感器控制"],
        "warn_modes": ["开环控制", "模型预测控制(MPC)"],
        "sensorless_method": "高频注入",
    },
}
