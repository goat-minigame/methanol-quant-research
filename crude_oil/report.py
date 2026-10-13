"""As-of facts, integrity gates and a portable SQLite + evidence backup."""
import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from methanol import sources as base
from .sources import CATALOG, connect


def audit(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('database integrity failed')
    for attempt in db.execute('SELECT * FROM ingest_attempt'):
        for path_key, hash_key in [('evidence_path', 'evidence_hash'), ('raw_path', 'raw_hash')]:
            if attempt[path_key]:
                if base.digest(Path(attempt[path_key]).read_bytes()) != attempt[hash_key]:
                    raise ValueError('evidence integrity failed')
        ev = json.loads(Path(attempt['evidence_path']).read_bytes())
        for key in ('raw_hash', 'ingested_at', 'started_at', 'http_status'):
            if ev.get(key) != attempt[key]:
                raise ValueError('attempt/evidence mismatch')
    for row in db.execute('''SELECT o.raw_hash,a.raw_hash AS attempt_hash FROM observation o
                             JOIN ingest_attempt a ON a.id=o.attempt_id'''):
        if row['raw_hash'] != row['attempt_hash']:
            raise ValueError('observation/evidence mismatch')


def status(db_path, as_of=None):
    cutoff = base.timestamp(as_of or base.utcnow())
    db = connect(db_path)
    try:
        audit(db)
        facts = [r for r in base.latest_observations(db, cutoff.isoformat()) if r['source'] in CATALOG]
        sources = []
        for name, (_, lineage, url, kind, tz, limit) in CATALOG.items():
            attempts = [dict(r) for r in db.execute('SELECT * FROM ingest_attempt WHERE source=?', (name,))
                        if base.timestamp(r['ingested_at']) <= cutoff]
            attempts.sort(key=lambda r: (base.timestamp(r['ingested_at']), r['id']))
            latest = attempts[-1] if attempts else None
            rows = [r for r in facts if r['source'] == name]
            latest_day = max((r['observed_end'] for r in rows), default=None)
            age = (cutoff.astimezone(ZoneInfo(tz)).date() - date.fromisoformat(latest_day)).days if latest_day else None
            sources.append(dict(source=name, lineage=lineage, url=url, category=kind, timezone=tz,
                availability=(latest['error_category'] or ('parsed_facts' if rows else 'unavailable')) if latest else 'unavailable',
                latest_attempt={k: latest[k] for k in ('id', 'http_status', 'ingested_at', 'error_category', 'inserted_count', 'raw_hash')} if latest else None,
                observation_count=len(rows), missing_value_count=sum(r['value'] is None for r in rows),
                latest_observation_date=latest_day, age_calendar_days=age,
                staleness='unknown' if age is None else 'stale' if age > limit else 'within_display_age_limit',
                age_limit_days=limit, independent_crosscheck=False))
        for row in facts:
            row['detail'] = json.loads(row['detail'])
            row['observation_date'] = row['observed_end']
            row['source_url'] = CATALOG[row['source']][2].format(day=row['observed_end'].replace('-', ''))
            row['research_eligible'] = False  # no new official-rules/schema approval implied
        dates = sorted({r['observed_end'] for r in facts if r['source'] == 'ine_sc'})
        gaps = [{'after': a, 'before': b, 'calendar_days': (date.fromisoformat(b)-date.fromisoformat(a)).days-1,
                 'classification': 'unclassified_calendar_gap_not_confirmed_missing_sessions'}
                for a, b in zip(dates, dates[1:]) if (date.fromisoformat(b)-date.fromisoformat(a)).days > 1]
        return dict(as_of=cutoff.isoformat(), sources=sources, facts=facts, calendar_gaps=gaps,
            calendar_status='official_holiday_calendar_unavailable_no_fill', events=[],
            counts_all_time={table: db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                for table in ('observation', 'ingest_attempt', 'forecast', 'forecast_verification')},
            forecast_enabled=False, cross_asset_signals_enabled=False, research_eligible_observations=0,
            eia_v2_acceptance='pending_authenticated_live_validation',
            gaps=['Authenticated EIA v2 live request and schema validation',
                  'SC current official contract rules page blocked by WAF; no independent crosscheck',
                  'Verified licensed Brent and WTI futures daily sources unavailable; spot is not futures',
                  'Precise publication timestamps and historical release/revision vintages unknown',
                  'Official holiday calendar and complete dated contract history unavailable',
                  'No newly verified OPEC+, Hormuz or diplomatic event evidence; events unavailable',
                  'No calibrated forecast or validated MA/PG/BU transmission evidence'],
            availability_policy='available_at >= complete response AND known publication AND reprocessing; no historical backfill')
    finally:
        db.close()


def backup(db_path, destination):
    """Create a new directory; copy all evidence and rewrite paths in backup only."""
    target = Path(destination).resolve()
    if target.exists():
        raise ValueError('backup destination must not exist')
    db = connect(db_path)
    try:
        audit(db)
        target.mkdir(parents=True)
        raw_dir = target / 'raw'
        raw_dir.mkdir()
        with sqlite3.connect(target / 'crude.sqlite3') as copy:
            db.backup(copy)
            for row in db.execute('SELECT id,evidence_path,raw_path FROM ingest_attempt'):
                paths = []
                for key in ('evidence_path', 'raw_path'):
                    if row[key]:
                        dest = raw_dir / Path(row[key]).name
                        shutil.copyfile(row[key], dest)
                        paths.append(str(dest))
                    else:
                        paths.append(None)
                copy.execute('UPDATE ingest_attempt SET evidence_path=?,raw_path=? WHERE id=?', (*paths, row['id']))
        manifest = {str(p.relative_to(target)): base.digest(p.read_bytes()) for p in target.rglob('*') if p.is_file()}
        (target / 'manifest.json').write_bytes(base.canonical(manifest))
        return dict(backup=str(target), files=len(manifest), status='complete')
    finally:
        db.close()


def restore(source, destination):
    """Verify manifest before restoring to a new directory; never overwrite a DB."""
    source, target = Path(source).resolve(), Path(destination).resolve()
    if target.exists():
        raise ValueError('restore destination must not exist')
    manifest = json.loads((source / 'manifest.json').read_bytes())
    if 'crude.sqlite3' not in manifest:
        raise ValueError('backup database absent')
    for name, expected in manifest.items():
        path = (source / name).resolve()
        if source not in path.parents or base.digest(path.read_bytes()) != expected:
            raise ValueError('backup manifest integrity failed')
    # Rebind paths after relocation before copying through the audited backup path.
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        staged = Path(temp) / 'crude.sqlite3'
        shutil.copyfile(source / 'crude.sqlite3', staged)
        with sqlite3.connect(staged) as db:
            for row in db.execute('SELECT id,evidence_path,raw_path FROM ingest_attempt').fetchall():
                paths = [str(source / 'raw' / Path(p).name) if p else None for p in row[1:]]
                db.execute('UPDATE ingest_attempt SET evidence_path=?,raw_path=? WHERE id=?', (*paths, row[0]))
        return backup(staged, target)
