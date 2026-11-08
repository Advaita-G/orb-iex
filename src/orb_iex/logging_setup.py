"""Logging: JSON lines to a daily file plus a readable console.

Every record carries variant=ORB-IEX. A filter masks any secret value that
somehow reaches a log message.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .constants import VARIANT

LOGGER_NAME = "orb_iex"


class RedactSecrets(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def add(self, *secrets: str) -> None:
        self.secrets.extend(s for s in secrets if s and len(s) >= 6)

    def _mask(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(record.getMessage())
        record.args = ()
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = json.loads(self._mask(json.dumps(fields, default=str)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "variant": VARIANT,
            "event": getattr(record, "event", None),
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["traceback"] = _redactor._mask(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        text = f"{ts} {record.levelname:<7} [{VARIANT}] {record.getMessage()}"
        if record.exc_info:
            text += "\n" + _redactor._mask(self.formatException(record.exc_info))
        return text


class DailyFileHandler(logging.FileHandler):
    """orb_iex-YYYYMMDD.jsonl, switching to a new file when the local date changes."""

    def __init__(self, log_dir: Path, retention_days: int = 30):
        self.log_dir = log_dir
        self.retention_days = retention_days
        self.day = datetime.now().strftime("%Y%m%d")
        super().__init__(log_dir / f"orb_iex-{self.day}.jsonl", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        day = datetime.now().strftime("%Y%m%d")
        if day != self.day:
            self.acquire()
            try:
                self.close()
                self.day = day
                self.baseFilename = str((self.log_dir / f"orb_iex-{day}.jsonl").resolve())
                self.stream = self._open()
                _rotate(self.log_dir, self.retention_days)
            finally:
                self.release()
        super().emit(record)


def _rotate(log_dir: Path, retention_days: int) -> None:
    try:
        from .ops import rotate_logs
        rotate_logs(log_dir, retention_days)
    except OSError:
        pass                                     # log housekeeping must never stop the bot


_redactor = RedactSecrets()


def setup_logging(data_dir: str | Path, level: str = "INFO", retention_days: int = 30) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _rotate(log_dir, retention_days)
    file_handler = DailyFileHandler(log_dir, retention_days)
    file_handler.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    for handler in (file_handler, console):
        handler.addFilter(_redactor)
        logger.addHandler(handler)
    return logger


def register_secrets(*secrets: str) -> None:
    _redactor.add(*secrets)


def log_event(logger: logging.Logger, level: int, event: str, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"event": event, "fields": fields})
