"""Run synthetic tests with sockets blocked, then exercise only local CLI paths."""
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def deny_network(*args, **kwargs):
    raise AssertionError('Network access is forbidden in the offline test suite')


def main():
    with patch.object(socket.socket, 'connect', deny_network), \
            patch.object(socket.socket, 'connect_ex', deny_network), \
            patch.object(socket, 'create_connection', deny_network):
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    with tempfile.TemporaryDirectory(prefix='methanol-synthetic-') as directory:
        db = str(Path(directory) / 'synthetic.sqlite3')
        for command, flag in [('sources', 'real_forecast_enabled'), ('industry', 'forecast_enabled')]:
            run = subprocess.run([sys.executable, '-m', 'methanol', command, 'status', '--db', db],
                                 cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
            assert json.loads(run.stdout)[flag] is False
        # This source is explicitly unavailable: it never makes HTTP requests.
        run = subprocess.run([sys.executable, '-m', 'methanol', 'sources', 'ingest',
                              '--db', db, '--raw-dir', directory, '--dates', '2026-10-12',
                              '--sources', 'port_inventory'], cwd=ROOT, capture_output=True,
                             text=True, timeout=30)
        assert run.returncode == 1
        result = json.loads(run.stdout)
        assert result['errors'][0]['error_category'] == 'unavailable'
        assert result['inserted_count'] == 0 and result['real_forecast_enabled'] is False
    print('Offline CLI checks passed; no live acquisition was performed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
