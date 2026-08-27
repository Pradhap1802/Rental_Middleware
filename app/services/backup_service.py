import os
import shutil
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from ..models.domain import BackupModel
from ..logging.logger import log_event


class BackupService:
    """
    Enterprise Backup & Disaster Recovery Service managing online SQLite backups,
    PRAGMA integrity verification, safety pre-restore snapshots, and retention purging.
    """
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.backup_dir = os.path.join(data_dir, "backups")
        os.makedirs(self.backup_dir, exist_ok=True)

    def verify_backup(self, backup_path: str) -> bool:
        """
        Validates SQLite backup file integrity using header magic bytes and PRAGMA quick_check.
        """
        if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
            return False

        # 1. Header Magic Bytes Validation
        try:
            with open(backup_path, "rb") as f:
                header = f.read(16)
                if not header.startswith(b"SQLite format 3"):
                    return False
        except Exception:
            return False

        # 2. SQLite Integrity PRAGMA Check
        try:
            conn = sqlite3.connect(backup_path)
            cur = conn.cursor()
            cur.execute("PRAGMA quick_check;")
            res = cur.fetchone()
            conn.close()
            return bool(res and str(res[0]).lower() == "ok")
        except Exception:
            return False

    def trigger_backup(self) -> Dict[str, Any]:
        """
        Executes online atomic SQLite backup, verifies integrity, and purges expired files.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        db_path = os.path.join(self.data_dir, "state.db")
        cfg_path = os.path.join(self.data_dir, "config.json.enc")

        db_backup_path = os.path.join(self.backup_dir, f"state_backup_{timestamp}.db")
        cfg_backup_path = os.path.join(self.backup_dir, f"config_backup_{timestamp}.json.enc")

        # Atomic online SQLite backup
        if os.path.exists(db_path):
            src_conn = sqlite3.connect(db_path)
            dest_conn = sqlite3.connect(db_backup_path)
            src_conn.backup(dest_conn)
            dest_conn.close()
            src_conn.close()

        if os.path.exists(cfg_path):
            shutil.copy2(cfg_path, cfg_backup_path)

        # Deliberately NOT backing up secret.key here: copying the decryption key into
        # the same directory as the encrypted config it decrypts defeats encryption at
        # rest for anyone who gains access to the backup folder. Recover the key from
        # its original provisioning source (the live .data/secret.key file, or the
        # RENTAL_MIDDLEWARE_SECRET_KEY env var) instead of from a backup snapshot.

        # Integrity Verification
        verified = self.verify_backup(db_backup_path)
        if not verified:
            log_event("Backup", f"Backup verification failed for {os.path.basename(db_backup_path)}")
            return {"status": "error", "message": "Backup created but failed integrity verification"}

        self.purge_old_backups(days=30, max_backups=10)
        log_event("Backup", f"State database backup created and verified: {os.path.basename(db_backup_path)}")
        return {
            "status": "success",
            "verified": True,
            "db_backup": os.path.basename(db_backup_path),
            "cfg_backup": os.path.basename(cfg_backup_path) if os.path.exists(cfg_backup_path) else None,
        }

    def restore_backup(self, backup_filename: str) -> Dict[str, Any]:
        """
        Restores database state from a verified backup file after creating a pre-restore safety snapshot.
        """
        backup_path = os.path.join(self.backup_dir, backup_filename)
        if not os.path.exists(backup_path):
            return {"status": "error", "message": f"Backup file '{backup_filename}' not found"}

        # 1. Verify backup integrity before restoring
        if not self.verify_backup(backup_path):
            return {"status": "error", "message": f"Backup file '{backup_filename}' failed integrity verification"}

        # 2. Create Pre-Restore Safety Snapshot
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        db_path = os.path.join(self.data_dir, "state.db")
        safety_path = os.path.join(self.backup_dir, f"state_prerestore_{timestamp}.db")

        if os.path.exists(db_path):
            src_conn = sqlite3.connect(db_path)
            safety_conn = sqlite3.connect(safety_path)
            src_conn.backup(safety_conn)
            safety_conn.close()
            src_conn.close()

        # 3. Restore backup into state.db
        b_conn = sqlite3.connect(backup_path)
        dest_conn = sqlite3.connect(db_path)
        b_conn.backup(dest_conn)
        dest_conn.close()
        b_conn.close()

        log_event("DisasterRecovery", f"Restored state database from backup '{backup_filename}' (Safety snapshot: {os.path.basename(safety_path)})")
        return {
            "status": "success",
            "message": f"Successfully restored database from '{backup_filename}'",
            "prerestore_snapshot": os.path.basename(safety_path),
        }

    def purge_old_backups(self, days: int = 30, max_backups: int = 10):
        """Purges old backup files exceeding retention age or file count limits."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        if not os.path.exists(self.backup_dir):
            return

        db_files = []
        for fname in os.listdir(self.backup_dir):
            fpath = os.path.join(self.backup_dir, fname)
            if os.path.isfile(fpath):
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
                if mtime < cutoff:
                    try:
                        os.remove(fpath)
                    except Exception:
                        pass
                elif fname.startswith("state_backup_") and fname.endswith(".db"):
                    db_files.append((fpath, mtime))

        # Enforce max backups limit (Keep latest max_backups files)
        db_files.sort(key=lambda x: x[1], reverse=True)
        if len(db_files) > max_backups:
            for fpath, _ in db_files[max_backups:]:
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
