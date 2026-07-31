"""设备运行状态机：所有危险运行意图的统一约束入口。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from PySide6.QtCore import QObject, Signal


class RuntimeState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    PRECHECK = "precheck"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    FAULT_LOCKED = "fault_locked"


class TransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class StateTransition:
    timestamp: str
    previous: RuntimeState
    current: RuntimeState
    reason: str


class RuntimeStateMachine(QObject):
    """全局唯一的设备状态权威。

    stateChanged 参数为 previous/current/reason。故障锁定会跨通信断开保持，
    必须显式 reset_fault()，从而避免重连动作偷偷清除故障。
    """

    stateChanged = Signal(object, object, str)
    connectionChanged = Signal(bool, str)
    transitionRejected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._state = RuntimeState.DISCONNECTED
        self._connected = False
        self._connection_note = ""
        self._history: list[StateTransition] = []
        self._lock = threading.RLock()

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def connection_note(self) -> str:
        return self._connection_note

    @property
    def history(self) -> tuple[StateTransition, ...]:
        return tuple(self._history)

    def connection_changed(self, connected: bool, note: str = "") -> None:
        with self._lock:
            self._connected = bool(connected)
            self._connection_note = note
            self.connectionChanged.emit(self._connected, note)
            if connected:
                if self._state is RuntimeState.DISCONNECTED:
                    self._transition(RuntimeState.CONNECTED, note or "通信已连接")
                # FAULT_LOCKED 重连后保持锁定，等待人工复位。
                return
            if self._state in (RuntimeState.RUNNING, RuntimeState.STOPPING):
                self._transition(RuntimeState.FAULT_LOCKED,
                                 note or "运行中通信断开，状态未知")
            elif self._state is not RuntimeState.FAULT_LOCKED:
                self._transition(RuntimeState.DISCONNECTED, note or "通信已断开")

    def begin_precheck(self, reason: str = "开始运行预检") -> None:
        with self._lock:
            self._require(RuntimeState.CONNECTED, "只有已连接设备可以开始预检")
            self._transition(RuntimeState.PRECHECK, reason)

    def pass_precheck(self, reason: str = "运行预检通过") -> None:
        with self._lock:
            self._require(RuntimeState.PRECHECK, "当前没有正在执行的预检")
            self._transition(RuntimeState.READY, reason)

    def fail_precheck(self, reason: str) -> None:
        with self._lock:
            self._require(RuntimeState.PRECHECK, "当前没有正在执行的预检")
            self._transition(RuntimeState.CONNECTED, reason or "运行预检未通过")

    def confirm_started(self, reason: str = "启动命令已执行") -> None:
        with self._lock:
            self._require(RuntimeState.READY, "设备未就绪，禁止启动")
            self._transition(RuntimeState.RUNNING, reason)

    def reject_start(self, reason: str = "设备拒绝启动命令") -> None:
        """明确的 START NACK 可权威撤销错误/残留的 RUNNING 状态。

        正常情况下等待 v2 ACK 时状态仍为 READY，因此本方法是 no-op；允许从
        RUNNING 回到 READY 是为了修复旧版本在发送成功时提前推进状态的残留。
        ACK 超时或链路中断不代表设备拒绝，不能调用本方法。
        """
        with self._lock:
            if self._state is RuntimeState.READY:
                return
            self._require(RuntimeState.RUNNING, "当前没有待撤销的启动过程")
            self._transition(RuntimeState.READY, reason)

    def request_stop(self, reason: str = "请求正常停机") -> None:
        with self._lock:
            self._require(RuntimeState.RUNNING, "设备不在运行状态")
            self._transition(RuntimeState.STOPPING, reason)

    def confirm_stopped(self, reason: str = "停机命令已执行") -> None:
        with self._lock:
            self._require(RuntimeState.STOPPING, "当前没有待确认的停机过程")
            self._transition(RuntimeState.READY, reason)

    def observe_device_stopped(self, reason: str = "遥测确认设备已停机") -> None:
        """设备安全逻辑可在没有上位机STOP命令时主动受控停机。"""
        with self._lock:
            if self._state not in (RuntimeState.RUNNING, RuntimeState.STOPPING):
                return
            self._transition(RuntimeState.READY, reason)

    def reject_stop(self, reason: str = "设备拒绝停机命令") -> None:
        """收到明确NACK时回到RUNNING；超时/链路未知不应调用本方法。"""
        with self._lock:
            self._require(RuntimeState.STOPPING, "当前没有待确认的停机过程")
            self._transition(RuntimeState.RUNNING, reason)

    def lock_fault(self, reason: str) -> None:
        with self._lock:
            if not reason.strip():
                raise ValueError("故障锁定必须提供原因")
            if self._state is RuntimeState.FAULT_LOCKED:
                return
            self._transition(RuntimeState.FAULT_LOCKED, reason.strip())

    def reset_fault(self, reason: str = "人工确认故障复位") -> None:
        with self._lock:
            self._require(RuntimeState.FAULT_LOCKED, "当前没有锁定故障")
            target = RuntimeState.CONNECTED if self._connected else RuntimeState.DISCONNECTED
            self._transition(target, reason)

    def _require(self, expected: RuntimeState, message: str) -> None:
        if self._state is expected:
            return
        detail = f"{message}（当前状态：{self._state.value}）"
        self.transitionRejected.emit(detail)
        raise TransitionError(detail)

    def _transition(self, target: RuntimeState, reason: str) -> None:
        previous = self._state
        if previous is target:
            return
        self._state = target
        record = StateTransition(
            timestamp=datetime.now().astimezone().isoformat(timespec="milliseconds"),
            previous=previous,
            current=target,
            reason=reason,
        )
        self._history.append(record)
        self.stateChanged.emit(previous, target, reason)
