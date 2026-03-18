from __future__ import annotations

import logging
import os


def setup_logging() -> logging.Logger:
    enabled = os.getenv("ENABLE_LOGGING", "0") == "1"
    if not enabled:
        logging.disable(logging.CRITICAL)
        return logging.getLogger("research-runner")

    verbose = os.getenv("VERBOSE_LOGGING", "0") == "1"
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return logging.getLogger("research-runner")
