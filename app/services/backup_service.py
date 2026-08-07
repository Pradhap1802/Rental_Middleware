import os
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from ..models.domain import BackupModel


class BackupService:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.backup_dir = os.path.join(data_dir, "backups")
        os.makedirs(self.backup_dir, exist_ok=True)

    def trigger_backup(self) -> Dict[str, str]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        db_path = os.path.join(self.data_dir, "state.db")
        cfg_path = os.path.join(self.data_dir, "config.json.enc")
        key_path = os.path.join(self.data_dir, "secret.key")

        db_backup_path = os.path.join(self.backup_dir, f"state_backup_{timestamp}.db")
        cfg_backup_path = os.path.join(self.backup_dir, f"config_backup_{timestamp}.json.enc")
        key_backup_path = os.path.join(self.backup_dir, f"key_backup_{timestamp}.key")

        # Online SQLite backup
        if os.path.exists(db_path):
            src_conn = sqlite3.connect(db_path)
            dest_conn = sqlite3.connect(db_backup_path)
            src_conn.backup(dest_conn)
            dest_conn.close()
            src_conn.close()

        if os.path.exists(cfg_path):
            shutil.copy2(cfg_path, cfg_backup_path)
        if os.path.exists(key_path):
            shutil.copy2(key_path, key_backup_path)

        self.purge_old_backups(days=30)
        return {
            "status": "success",
            "db_backup": os.path.basename(db_backup_path),
            "cfg_backup": os.path.basename(cfg_backup_path),
        }

    def purge_old_backups(self, days: int = 30):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        if not os.path.exists(self.backup_dir):
            return
        for fname in os.listdir(self.backup_dir):
            fpath = os.path.join(self.backup_dir, fname)
            if os.path.isfile(fpath):
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
                if mtime < cutoff:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass

    def list_backups(self) -> List[BackupModel]:
        results = []
        if not os.path.exists(self.backup_dir):
            return results
        for fname in os.listdir(self.backup_dir):
            fpath = os.path.join(self.backup_dir, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                created_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                results.append(BackupModel(filename=fname, size_bytes=stat.st_size, created_at=created_at))
        return sorted(results, key=lambda x: x.created_at, reverse=True)
