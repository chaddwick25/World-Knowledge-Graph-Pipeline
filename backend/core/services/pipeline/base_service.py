import abc
import logging
from typing import Dict, Any

class BaseService(abc.ABC):
    """
    An abstract base class that defines the contract for all registered services.
    
    Each service must implement the `execute` method.
    """
    def __init__(self):
        # Provide a dedicated logger for each service instance
        self.logger = logging.getLogger(self.__class__.__name__)

    def setup(self, config: Dict[str, Any]):
        """
        (Optional) A hook for setting up resources before execution.
        This can be overridden by subclasses if needed.
        """
        self.logger.info("Performing generic service setup...")
        pass

    @abc.abstractmethod
    def execute(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        The main entry point for the service. This method MUST be implemented
        by all subclasses.
        
        Args:
            config: A dictionary containing all parameters for the execution.

        Returns:
            A dictionary containing the results of the service execution.
        """
        raise NotImplementedError("Each service must implement the 'execute' method.")

    def teardown(self):
        """
        (Optional) A hook for cleaning up resources after execution.
        This can be overridden by subclasses if needed.
        """
        self.logger.info("Performing generic service teardown...")
        pass
