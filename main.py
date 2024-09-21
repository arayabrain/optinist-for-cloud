# run backend main module.
from studio.app.common.core.logger import AppLogger

logger = AppLogger.get_logger()

if __name__ == "__main__":
    from studio.__main_unit__ import main

    logger.info("Starting Optinist application")
    main(develop_mode=True)
