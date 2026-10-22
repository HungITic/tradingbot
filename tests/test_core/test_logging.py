"""Error-only logging, bounded retention and daemon startup failure capture."""
from datetime import datetime, timedelta
import logging
import os
import signal
import subprocess
import sys
import time

from app.core.log_files import ErrorFileHandler, prune_old_logs


def test_cleanup_only_removes_expired_owned_logs(tmp_path):
    now = time.time()
    for name in ['bot.log', 'bot_error.log.2020-01-01', 'audit.json', 'userbot.session', 'other.log']:
        path = tmp_path / name
        path.write_text('keep financial data')
        os.utime(path, (now - 4 * 86400, now - 4 * 86400))
    recent = tmp_path / 'bot_error.log.2020-01-02'
    recent.write_text('recent errors')
    prune_old_logs(tmp_path, now=now)
    assert not (tmp_path / 'bot.log').exists()
    assert not (tmp_path / 'bot_error.log.2020-01-01').exists()
    assert recent.exists()
    assert all((tmp_path / name).exists() for name in ['audit.json', 'userbot.session', 'other.log'])


def test_maintenance_rotates_quiet_log_and_keeps_only_two_backups(tmp_path, monkeypatch):
    now = time.time()
    today = datetime.now().date()
    for days in range(1, 5):
        path = tmp_path / f'bot_error.log.{today - timedelta(days=days)}'
        path.write_text(f'errors {days} days ago')
        os.utime(path, (now - days * 86400, now - days * 86400))
    handler = ErrorFileHandler(tmp_path, maintenance=False)
    try:
        assert len(list(tmp_path.glob('bot_error.log.*'))) == 2
        record = logging.LogRecord('test', logging.ERROR, __file__, 0, 'first error', (), None)
        handler.handle(record)
        rollover = handler.rolloverAt
        monkeypatch.setattr(time, 'time', lambda: rollover + 1)
        handler.maintain()  # No new error is required to rotate.
        handler.handle(logging.LogRecord('test', logging.ERROR, __file__, 0, 'next error', (), None))
        assert (tmp_path / 'bot_error.log').read_text() == 'next error\n'
        assert len(list(tmp_path.glob('bot_error.log.*'))) <= 2
    finally:
        handler.close()


def test_debug_env_and_child_logger_cannot_emit_non_error_records(tmp_path):
    script = '''
import logging, sys, structlog
from pathlib import Path
from app.core.logging import setup_logging
setup_logging(Path(sys.argv[1]))
logging.info("discard-info")
structlog.get_logger("app.test").warning("discard-warning")
child = logging.getLogger("sqlalchemy.engine.Engine")
child.setLevel(logging.DEBUG)
child.info("discard-sql")
structlog.get_logger("app.test").error("keep-error")
logging.getLogger("telethon.network.mtprotosender").error("keep-telegram-error")
try:
    raise ValueError("keep-traceback")
except ValueError:
    logging.exception("keep-exception")
from app.config.database import engine
assert engine.echo is False
logging.shutdown()
'''
    environment = dict(os.environ, DEBUG='true', LOG_LEVEL='DEBUG')
    environment.pop('BOT_LOG_EXTERNAL', None)
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
        env=environment, text=True, capture_output=True, check=True)
    saved = (tmp_path / 'bot_error.log').read_text()
    assert result.stdout == ''
    for output in (saved, result.stderr):
        assert 'discard-' not in output
        assert all(message in output for message in ['keep-error', 'keep-telegram-error', 'keep-exception', 'keep-traceback'])


def test_daemon_captures_prestartup_errors_without_stdout_noise(tmp_path):
    from scripts.run_logged import main
    child = tmp_path / 'failed_start.py'
    child.write_text('import sys\nprint("discard-banner")\nraise RuntimeError("startup-error")\n')
    log_directory = tmp_path / 'logs'
    assert main(child, log_directory) == 1
    saved = (log_directory / 'bot_error.log').read_text()
    assert 'startup-error' in saved and 'Traceback' in saved
    assert 'discard-banner' not in saved


def test_daemon_writes_each_application_error_once(tmp_path):
    from scripts.run_logged import main
    child = tmp_path / 'application.py'
    child.write_text('from app.core.logging import setup_logging\n'
                     'import logging\nsetup_logging()\n'
                     'logging.error("single-error-record")\n')
    log_directory = tmp_path / 'logs'
    assert main(child, log_directory) == 0
    saved = (log_directory / 'bot_error.log').read_text()
    assert saved.count('single-error-record') == 1


def test_daemon_forwards_termination_and_saves_shutdown_error(tmp_path):
    child = tmp_path / 'child.py'
    child.write_text('import signal, sys, time\n'
                     'def stop(*args):\n'
                     '    print("shutdown-error", file=sys.stderr, flush=True)\n'
                     '    sys.exit(0)\n'
                     'signal.signal(signal.SIGTERM, stop)\n'
                     'print("child-ready", file=sys.stderr, flush=True)\n'
                     'time.sleep(5)\n')
    log_directory = tmp_path / 'logs'
    command = ('from pathlib import Path; import sys; from scripts.run_logged import main; '
               'sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))')
    launcher = subprocess.Popen([sys.executable, '-c', command, str(child), str(log_directory)])
    saved = log_directory / 'bot_error.log'
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if saved.exists() and 'child-ready' in saved.read_text():
                break
            time.sleep(.01)
        else:
            raise AssertionError('child failed to start')
        launcher.send_signal(signal.SIGTERM)
        assert launcher.wait(timeout=3) == 0
        assert 'shutdown-error' in saved.read_text()
    finally:
        if launcher.poll() is None:
            launcher.terminate()
            launcher.wait(timeout=6)
