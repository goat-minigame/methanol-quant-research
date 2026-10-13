"""Run explicit acquisition, local status/report, or evidence backup/restore."""
import argparse
import json
from pathlib import Path
from . import sources
from . import report as reporting


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    source = commands.add_parser('sources').add_subparsers(dest='action', required=True)
    ingest = source.add_parser('ingest')
    ingest.add_argument('--dates', nargs='+', required=True)
    ingest.add_argument('--sources', nargs='+', choices=list(sources.CATALOG))
    ingest.add_argument('--raw-dir', default='data/crude/raw')
    ingest.add_argument('--timeout', type=float, default=20)
    ingest.add_argument('--retries', type=int, default=1)
    status = source.add_parser('status')
    report = commands.add_parser('report')
    report.add_argument('--output', default='reports/crude/report.json')
    for command in (ingest, status, report):
        command.add_argument('--db', default='data/crude/crude.sqlite3')
    for command in (status, report):
        command.add_argument('--as-of')
    backup = commands.add_parser('backup')
    backup.add_argument('--db', default='data/crude/crude.sqlite3')
    backup.add_argument('--destination', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--source', required=True)
    restore.add_argument('--destination', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'backup':
            result = reporting.backup(args.db, args.destination)
        elif args.command == 'restore':
            result = reporting.restore(args.source, args.destination)
        elif args.command == 'sources' and args.action == 'ingest':
            result = sources.ingest(args.db, args.raw_dir, args.dates, args.sources, args.timeout, args.retries)
        else:
            result = reporting.status(args.db, args.as_of)
            if args.command == 'report':
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if result.get('errors') else 0
    except (ValueError, OSError, KeyError) as exc:
        # No transport URL or credential-bearing exception can escape here.
        print(json.dumps({'error': type(exc).__name__, 'status': 'failed', 'forecast_enabled': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
