# logging_config.py

import logging.config
from config import Config

def setup_logging(config: Config):
    logging_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'standard': {
                'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            },
            'json': {  # Optional: JSON formatter for structured logging
                'format': '{"time": "%(asctime)s", "name": "%(name)s", "level": "%(levelname)s", "message": "%(message)s"}',
                'class': 'logging.Formatter',
            },
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'standard',
                'level': config.logging.level.upper(),
                'stream': 'ext://sys.stdout',
            },
            'file': {
                'class': 'logging.FileHandler',
                'formatter': 'standard',
                'level': config.logging.level.upper(),
                'filename': config.logging.log_file,
                'mode': 'a',
            },
        },
        'loggers': {
            'DataLockerLogger': {
                'handlers': ['console', 'file'],
                'level': config.logging.level.upper(),
                'propagate': False,
            },
            'ReportGeneratorLogger': {
                'handlers': ['console', 'file'],
                'level': config.logging.level.upper(),
                'propagate': False,
            },
            # Add other loggers as needed
        }
    }

    logging.config.dictConfig(logging_config)
