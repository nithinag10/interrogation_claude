from __future__ import annotations

import logging
import os


def setup_logging() -> logging.Logger:
    verbose = os.getenv("VERBOSE_RUNNER", "1") == "1"
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return logging.getLogger("research-runner")
