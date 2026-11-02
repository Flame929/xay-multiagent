"""账本的每日 SQLite 备份。"""
import re
import sqlite3
from datetime import date
from pathlib import Path

_BACKUP_NAME = re.compile(r'^xay-(\d{8})\.db$')


def _date_text(today):
    if today is None:
        return date.today().strftime('%Y%m%d')
    if hasattr(today, 'strftime'):
        return today.strftime('%Y%m%d')
    text = str(today)
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return text.replace('-', '')
    if re.fullmatch(r'\d{8}', text):
        return text
    raise ValueError('today 要写 YYYYMMDD 或 YYYY-MM-DD')


def backup_daily(db_path, dest_dir=None, keep=7, today=None):
    """用 SQLite VACUUM INTO 做每日备份，并清理过期备份。"""
    db = Path(db_path)
    dest = db.parent / 'backups' if dest_dir is None else Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / ('xay-%s.db' % _date_text(today))
    if target.exists():
        return None

    source = sqlite3.connect(str(db))
    try:
        source.execute('VACUUM INTO ?', (str(target),))
    finally:
        source.close()

    limit = max(0, int(keep))
    backups = []
    for path in dest.iterdir():
        match = _BACKUP_NAME.fullmatch(path.name)
        if match and path.is_file():
            backups.append((match.group(1), path))
    backups.sort(key=lambda item: item[0])
    for _, path in backups[:-limit] if limit else backups:
        path.unlink()
    return target
