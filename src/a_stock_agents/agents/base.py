from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Settings
from ..contracts import AgentResult


class BaseAgent(ABC):
    name: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def run(self) -> AgentResult:
        raise NotImplementedError
