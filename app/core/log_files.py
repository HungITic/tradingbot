"""Daily error files and retention, independent of app settings and broker startup."""
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import re
import threading
import time

LOG_DIRECTORY = Path(__file__).resolve().parents[2] / 'logs'
LOG_FILENAME = 'bot_error.log'
RETENTION_DAYS = 3
_OWNED_LOG = re.compile(r'bot(?:_error)?\.log(?:\.\d{4}-\d{2}-\d{2})?\Z')


def prune_old_logs(directory: Path, *, now: float | None = None):
    """Delete only bot log files older than the retention window, never other artifacts."""
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * 86400
    for path in directory.iterdir():
        if _OWNED_LOG.fullmatch(path.name) and path.name != LOG_FILENAME:
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except FileNotFoundError:
                pass


class ErrorFileHandler(TimedRotatingFileHandler):
    """Rotate at midnight; retain today's file and two daily backups.

    The maintenance thread also runs during quiet periods. The logging lock keeps
    rotation and writes mutually exclusive; close stops maintenance on shutdown.
    """
    def __init__(self, directory: Path = LOG_DIRECTORY, *, maintenance: bool = True):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        super().__init__(directory / LOG_FILENAME, when='midnight', backupCount=2,
                         encoding='utf-8', delay=True)
        self.setLevel(logging.ERROR)
        self._stop_maintenance = threading.Event()
        self.maintain()
        if maintenance:
            threading.Thread(target=self._maintenance_loop, name='error-log-retention', daemon=True).start()

    def maintain(self):
        with self.lock:
            if self._closed:
                return
            if time.time() >= self.rolloverAt:
                self.doRollover()
            for filename in self.getFilesToDelete():
                Path(filename).unlink(missing_ok=True)
            prune_old_logs(Path(self.baseFilename).parent)

    def _maintenance_loop(self):
        while not self._stop_maintenance.wait(60):
            try:
                self.maintain()
            except OSError:
                logging.getLogger(__name__).exception('error_log_retention_failed')

    def close(self):
        self._stop_maintenance.set()
        super().close()
