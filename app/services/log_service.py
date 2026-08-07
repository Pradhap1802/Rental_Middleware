import os
from typing import List, Optional
from ..logging.logger import MAIN_LOG_PATH


class LogService:
    @staticmethod
    def get_recent_logs(lines: int = 100) -> List[str]:
        if not os.path.exists(MAIN_LOG_PATH):
            return ["Log file not initialized yet."]
        try:
            with open(MAIN_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                all_lines = f.readlines()
                return all_lines[-lines:]
        except Exception as e:
            return [f"Error reading log file: {str(e)}"]

    @staticmethod
    def get_log_download_path() -> Optional[str]:
        if os.path.exists(MAIN_LOG_PATH):
            return MAIN_LOG_PATH
        return None
