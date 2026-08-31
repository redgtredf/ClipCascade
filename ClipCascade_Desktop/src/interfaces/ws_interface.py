from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from core.config import Config

if TYPE_CHECKING:
    # Type-only: importing the CLI tray at runtime here would drag CLI-only
    # dependencies into every platform build that imports this interface.
    from cli.tray import TaskbarPanel


class WSInterface(ABC):
    ############################
    #    ABSTRACT VARIABLES    #
    ############################
    is_auto_reconnecting: bool = False

    @abstractmethod
    def __init__(self, config: Config, is_login_phase=True):
        """This method must be implemented in subclasses."""
        pass

    @abstractmethod
    def get_total_timeout(self):
        """This method must be implemented in subclasses."""
        pass

    @abstractmethod
    def get_stats(self):
        """This method must be implemented in subclasses."""
        pass

    @abstractmethod
    def connect(self):
        """This method must be implemented in subclasses."""
        pass

    @abstractmethod
    def set_tray_ref(self, sys_tray: "TaskbarPanel"):
        """This method must be implemented in subclasses."""
        pass

    @abstractmethod
    def manual_reconnect(self):
        """This method must be implemented in subclasses."""
        pass

    @abstractmethod
    def disconnect(self):
        """This method must be implemented in subclasses."""
        pass
