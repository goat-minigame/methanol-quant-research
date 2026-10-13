"""Independent CLI to avoid coupling to shared CLI and other ingestion work."""
import argparse
import json
from pathlib import Path
from .eod import ingest, status, backup


def main():
    p = argparse.ArgumentParser(description='MA official daily observations; no buy signals')
    p.add_argument('--db', default='data/options/options.sqlite3')
    p.add_argument('--raw-dir', default='data/options/raw')
    sub = p.add_subparsers(dest='command', required=True)
    i = sub.add_parser('ingest')
    i.add_argument('--date', required=True)
    i.add_argument('--timeout', type=float, default=20)
    for name in ('status', 'report'):
        s = sub.add_parser(name)
        s.add_argument('--as-of')
        s.add_argument('--output')
    b = sub.add_parser('backup')
    b.add_argument('destination')
    a = p.parse_args()
    try:
        if a.command == 'ingest':
            result = ingest(a.db, a.raw_dir, a.date, a.timeout)
            failed = any(r['error'] for r in result)
        elif a.command == 'backup':
            result = {'backup': backup(a.db, a.raw_dir, a.destination)}
            failed = False
        else:
            result = status(a.db, a.raw_dir, a.as_of)
            failed = bool(result['integrity_errors'])
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if getattr(a, 'output', None):
            path = Path(a.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + '\n', encoding='utf-8')
        else:
            print(text)
        return int(failed)
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
