import threading
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class SingletonBase:
    """
    A generic, thread-safe Singleton base class using the __new__ method.

    Subclasses inheriting from this will ensure only one instance of that
    specific subclass is created. Subclass __init__ methods MUST check the
    ``_singleton_initialized`` flag and return immediately when True so that
    repeated ``Class()`` calls never reset already-initialised state:

        def __init__(self):
            if getattr(self, '_singleton_initialized', False):
                return
            # ... one-time init ...
            self._singleton_initialized = True
    """
    _instances: dict = {}  # Dictionary to store instances keyed by class type
    _lock = threading.Lock()  # Lock for thread-safe instance creation

    def __new__(cls, *args, **kwargs):
        # Use double-checked locking for efficiency
        if cls not in cls._instances:  # First check (no lock)
            with cls._lock:  # Lock acquired only if instance might not exist
                # Second check (inside lock)
                if cls not in cls._instances:
                    # Create the new instance using the standard __new__
                    instance = super().__new__(cls)
                    # Store the instance in the class-specific dictionary
                    cls._instances[cls] = instance
                    # Initialise the guard flag to False *before* __init__ is
                    # called so every subclass __init__ can check it reliably.
                    instance._singleton_initialized = False

        return cls._instances[cls]
