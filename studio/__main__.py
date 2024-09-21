from fastapi import FastAPI

from studio.__main_unit__ import main
from studio.app.common.core.logger import AppLogger

# Initialize the logger
logger = AppLogger.get_logger()

# Create FastAPI app
app = FastAPI()


# Add health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# run backend main module.
if __name__ == "__main__":
    logger.info("Starting Optinist application")
    main()
