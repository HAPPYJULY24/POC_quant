import json
import os
from datetime import datetime, time
from typing import Dict, Any, List
from src.utils import to_kl_time, get_kl_time

class ConfigManager:
    def __init__(self, config_path: str = None):
        if config_path is None:
            # Default path relative to workspace
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            config_path = os.path.join(base_dir, "config", "settings.json")
        
        self.config_path = config_path
        self.settings = self._load_settings()

    def _load_settings(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found at {self.config_path}")
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    @property
    def system(self) -> Dict[str, Any]:
        return self.settings.get("system_settings", {})

    @property
    def account(self) -> Dict[str, Any]:
        return self.settings.get("account_settings", {})

    @property
    def strategy(self) -> Dict[str, Any]:
        return self.settings.get("strategy_settings", {})

    @property
    def sessions(self) -> Dict[str, Any]:
        return self.settings.get("trading_sessions", {})

    def is_market_open(self, dt: datetime = None) -> bool:
        """Check if the provided datetime (default: now) falls within active trading sessions in KL."""
        if dt is None:
            dt = get_kl_time()
        else:
            dt = to_kl_time(dt)

        # Check weekend
        if self.sessions.get("ignore_weekends", True):
            if dt.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
                return False

        # Get active sessions
        active_sessions = self.sessions.get("fcpo_active_sessions", [])
        current_time = dt.time()

        for session in active_sessions:
            start_str = session["start"]
            end_str = session["end"]
            
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))
            
            start_time = time(start_h, start_m)
            end_time = time(end_h, end_m)
            
            if start_time <= current_time <= end_time:
                return True

        return False
