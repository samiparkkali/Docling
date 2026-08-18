from __future__ import annotations

import logging
import os
import sys

_HEX_COLORS = {
    logging.DEBUG: "#4385ef",     # blue
    logging.INFO: "#50c338",      # green
    logging.WARNING: "#e5760b",   # yellow
    logging.ERROR: "#E06C75",     # red
    logging.CRITICAL: "#BE5046",  # dark red
}
_RESET = "\033[0m"


def _hex_to_ansi(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return f"\033[38;2;{r};{g};{b}m"


_COLORS = {level: _hex_to_ansi(hex_color) for level, hex_color in _HEX_COLORS.items()}


class _ColorFormatter(logging.Formatter):
    def __init__(self, use_color: bool) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self.use_color:
            return message
        color = _COLORS.get(record.levelno, "")
        return f"{color}{message}{_RESET}"


def get_logger(name: str, level: int | str = logging.INFO) -> logging.Logger:
    """
    Console logger with per-level colors. Level can also be set via
    the LOG_LEVEL env var (e.g. LOG_LEVEL=DEBUG), which takes precedence.
    """
    logger = logging.getLogger(name)
    logger.setLevel(os.environ.get("LOG_LEVEL", level))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_ColorFormatter(use_color=sys.stdout.isatty()))
        logger.addHandler(handler)
        logger.propagate = False

    return logger
