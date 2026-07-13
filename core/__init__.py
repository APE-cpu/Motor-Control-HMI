"""跨页面共享的核心运行服务。"""

from .runtime_state import RuntimeState, RuntimeStateMachine, TransitionError

__all__ = ["RuntimeState", "RuntimeStateMachine", "TransitionError"]
