# teloude/infrastructure/logging.py

import logging
from typing import TYPE_CHECKING
from ..config import AppConfig # Use relative import if module structure is defined

def setup_logging(config: 'AppConfig') -> None:
    """Initializes structured application logging based on configuration."""
    
    # Basic console handler setup (structured logging simulation)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging._nameToLevel.get(config.log_level.upper(), logging.INFO))
    
    # Prevent multiple handlers if called more than once (good practice)
    if not root_logger.handlers:
        root_logger.addHandler(handler)
    
    logging.info("Logging system initialized successfully.")