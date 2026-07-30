import os
import logging
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler

LOGS_DIR = Path(__file__).parent / 'logs'
LOGS_DIR.mkdir(exist_ok=True)


#  TODO: refactor this
def setup_pipeline_logger(pipeline_run_id: str, country_iso: str = "") -> str:
    """Setup the consolidated pipeline run logger.
    
    All stages (0–5) write to a single file per run:
        logs/pipeline/pipeline_{COUNTRY}_{run_short}_{timestamp}.log
    
    This replaces the old per-stage loggers (gv_nle_*.log, uslp_*.log, etc.)
    
    Args:
        pipeline_run_id: UUID for this pipeline run
        country_iso: ISO code for the country being processed
    
    Returns:
        Absolute path to the log file
    """
    log_dir = LOGS_DIR / 'pipeline'
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    run_short = (pipeline_run_id or "unknown")[:8]
    country_tag = (country_iso or "XX").upper()
    log_file = log_dir / f'pipeline_{country_tag}_{run_short}_{timestamp}.log'

    logger = logging.getLogger('pipeline')
    logger.setLevel(logging.INFO)

    # Replace existing file handlers so each run gets its own file
    logger.handlers = [
        h for h in logger.handlers
        if not isinstance(h, RotatingFileHandler)
    ]

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=100 * 1024 * 1024,  # 100 MB
        backupCount=3,
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    )
    logger.addHandler(file_handler)

    # Add console handler if none exists (Celery captures stdout)
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        logger.addHandler(console_handler)

    logger.info("Pipeline run log initialized: %s", log_file)
    return str(log_file)


# Backward-compatible alias for planet_initialization_service
setup_logger = setup_pipeline_logger


# Backward-compatible aliases for module-level loggers
gv_nle_logger = logging.getLogger('pipeline')
pipeline_logger = logging.getLogger('pipeline')
test_logger = logging.getLogger('pipeline')
nca_alignment_logger = logging.getLogger('pipeline')
uslp_logger = logging.getLogger('pipeline')
