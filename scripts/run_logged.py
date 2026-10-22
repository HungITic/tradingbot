"""Daemon launcher: capture child stderr, including failures before app startup."""
import logging
from pathlib import Path
import signal
import subprocess
import sys
import os

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
from app.core.log_files import ErrorFileHandler, LOG_DIRECTORY


def main(entrypoint=None, log_directory=LOG_DIRECTORY):
    handler = ErrorFileHandler(log_directory)
    handler.setFormatter(logging.Formatter('%(message)s'))
    child = None
    previous_handlers = {}
    def record(message, *, exc_info=None):
        handler.handle(logging.LogRecord('daemon', logging.ERROR, __file__, 0, message, (), exc_info))
    def forward(signum, frame):
        if child is not None and child.poll() is None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass
    try:
        environment = dict(os.environ, BOT_LOG_EXTERNAL='1', PYTHONUNBUFFERED='1')
        environment['PYTHONPATH'] = str(PROJECT_DIR) + os.pathsep + environment.get('PYTHONPATH', '')
        child = subprocess.Popen([sys.executable, str(entrypoint or PROJECT_DIR / 'run.py')],
            cwd=PROJECT_DIR, env=environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, forward)
        for line in child.stderr:
            if line.strip():
                record(line.rstrip('\n'))
        code = child.wait()
        return code if code >= 0 else 128 - code
    except Exception:
        record('Bot launcher failed', exc_info=sys.exc_info())
        return 1
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        if child is not None and child.stderr is not None:
            child.stderr.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        handler.close()


if __name__ == '__main__':
    raise SystemExit(main())
