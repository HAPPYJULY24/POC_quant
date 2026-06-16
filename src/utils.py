import sys
import codecs

if sys.stdout and sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        try:
            sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
        except Exception:
            pass

if sys.stderr and sys.stderr.encoding != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        try:
            sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())
        except Exception:
            pass

import logging
import os
from datetime import datetime
import pytz

# Setup Timezone
KL_TZ = pytz.timezone("Asia/Kuala_Lumpur")

def get_kl_time() -> datetime:
    """Return the current time in Asia/Kuala_Lumpur timezone."""
    return datetime.now(KL_TZ)

def to_kl_time(dt: datetime) -> datetime:
    """Convert any timezone-aware datetime to Kuala Lumpur time, or localize naive datetime."""
    if dt.tzinfo is None:
        return KL_TZ.localize(dt)
    return dt.astimezone(KL_TZ)

def parse_datetime(dt_str: str) -> datetime:
    """Parse YYYY-MM-DD HH:MM:SS string to a KL localized datetime."""
    naive_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
    return KL_TZ.localize(naive_dt)

def format_datetime(dt: datetime) -> str:
    """Format a datetime to standard string format YYYY-MM-DD HH:MM:SS."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def setup_logger(name: str = "poc_quant", log_level: str = "INFO") -> logging.Logger:
    """Create a logger that outputs to both console and a log file with beautiful formatting."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    if not logger.handlers:
        formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Console Handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # File Handler
        log_file = "poc_quant.log"
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    return logger
