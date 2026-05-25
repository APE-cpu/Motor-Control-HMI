"""所有通信驱动的抽象基类。"""
from abc import ABC, abstractmethod
from typing import Optional


class BaseComm(ABC):
    name: str = "BaseComm"

    @abstractmethod
    def open(self, **cfg) -> bool:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @abstractmethod
    def is_open(self) -> bool:
        ...

    @abstractmethod
    def send(self, data: bytes) -> int:
        ...

    @abstractmethod
    def recv(self, size: int = 64, timeout: Optional[float] = None) -> bytes:
        ...
