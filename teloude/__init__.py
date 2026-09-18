# Initialization file for the Teloude package
# Contains package-level setup and entry points.
# This is where main application services are initialized before the UI launches.

from teloude.config import AppConfig # Placeholder - Will be implemented

def initialize_application():
    """
    Main initialization routine run at startup.
    Order: Config -> Logging -> DB -> Core Services.
    """
    print("--- Teloude Initialization Starting ---")
    
    # 1. Load Configuration
    try:
        config = AppConfig()
        print("[INFO] Configuration loaded successfully.")
    except Exception as e:
        print(f"[FATAL] Failed to load configuration: {e}")
        return False

    # 2. Initialize Logging (uses config)
    from teloude.infrastructure.logging import setup_logging
    setup_logging(config)
    
    # 3. Initialize SQLite and run migrations
    from teloude.infrastructure.database import DatabaseManager
    db_manager = DatabaseManager()
    if not db_manager.initialize():
        print("[FATAL] Failed to initialize database.")
        return False

    # 4. Core Services Setup (e.g., FileScanner, etc.)
    # Placeholder for setting up core services that depend on DB/Config
    print("[INFO] All foundational layers initialized successfully.")
    return True