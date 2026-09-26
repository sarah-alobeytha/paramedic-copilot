"""Upgrade an existing Maya database, without starting voice or a web server."""
import argparse
from pathlib import Path
from maya_adapter import MayaAdapter

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True,help='Path to an existing Maya SQLite database')
    args=parser.parse_args()
    path=Path(args.db).resolve()
    if not path.is_file():parser.error('Database not found; check the path. No file was created.')
    adapter=MayaAdapter(path)
    with adapter.connect() as db:
        migration=db.execute('SELECT * FROM maya_schema_migrations ORDER BY version DESC LIMIT 1').fetchone()
        print('Database:',path)
        print('Schema version:',migration['version'])
        print('Backup:',migration['backup_path'] or 'No existing cases required a backup')
        print('Cases:',db.execute('SELECT COUNT(*) FROM cases').fetchone()[0])
        print('Structured measurements:',db.execute('SELECT COUNT(*) FROM vital_measurements').fetchone()[0])
