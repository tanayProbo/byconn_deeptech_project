import time
import logging
from typing import Dict, Optional

logger = logging.getLogger("byconnx.core.session_pool")

class ScrapingSession:
    """Stores session configuration, user cookies, and usage statistics."""
    def __init__(self, session_id: str, proxy: Optional[str] = None):
        self.session_id = session_id
        self.proxy = proxy
        self.cookies: list = []
        self.headers: Dict[str, str] = {}
        self.error_count = 0
        self.success_count = 0
        self.created_at = time.time()
        self.last_used = time.time()

    def record_success(self):
        self.success_count += 1
        self.last_used = time.time()

    def record_error(self):
        self.error_count += 1
        self.last_used = time.time()

    @property
    def score(self) -> float:
        """Calculates session health score based on success metrics."""
        total = self.success_count + self.error_count
        if total == 0:
            return 1.0
        return self.success_count / total


class SessionPool:
    """
    Manages and stores browser session objects.
    Re-uses good sessions and discards blacklisted proxies or high-error agents.
    """
    def __init__(self, max_sessions: int = 50, session_max_errors: int = 5):
        self.sessions: Dict[str, ScrapingSession] = {}
        self.max_sessions = max_sessions
        self.session_max_errors = session_max_errors

    def get_session(self, session_id: str) -> ScrapingSession:
        """Fetches an existing session or provisions a new one."""
        if session_id in self.sessions:
            session = self.sessions[session_id]
            if session.error_count >= self.session_max_errors:
                logger.warning(f"Discarding unhealthy session {session_id} due to errors.")
                self.sessions.pop(session_id)
            else:
                return session

        new_sess = ScrapingSession(session_id)
        if len(self.sessions) >= self.max_sessions:
            # Evict the oldest/worst session
            worst_id = min(self.sessions.keys(), key=lambda k: self.sessions[k].score)
            self.sessions.pop(worst_id)
            logger.info(f"Evicted low-score/stale session: {worst_id}")

        self.sessions[session_id] = new_sess
        return new_sess

    def update_session(self, session_id: str, cookies: list, headers: Dict[str, str]):
        """Persists extracted headers and cookies back to the session registry."""
        if session_id in self.sessions:
            self.sessions[session_id].cookies = cookies
            self.sessions[session_id].headers = headers
