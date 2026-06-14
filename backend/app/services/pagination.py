"""
Pagination state manager for OData queries.
Tracks cursor position for paginated data retrieval.
"""
import time
from typing import Any, Dict, Optional
from loguru import logger


class PaginationState:
    def __init__(self, base_url: str, total_count: int, page_size: int = 50):
        self.base_url = base_url
        self.total_count = total_count
        self.page_size = page_size
        self.current_offset = 0
        self.current_page = 1
        self.total_pages = max(1, (total_count + page_size - 1) // page_size)
        self.created_at = time.time()
        self.last_accessed = time.time()

    def get_skip_top(self) -> tuple:
        return self.current_offset, self.page_size

    def next_page(self) -> bool:
        if self.current_offset + self.page_size < self.total_count:
            self.current_offset += self.page_size
            self.current_page += 1
            self.last_accessed = time.time()
            return True
        return False

    def prev_page(self) -> bool:
        if self.current_offset > 0:
            self.current_offset = max(0, self.current_offset - self.page_size)
            self.current_page -= 1
            self.last_accessed = time.time()
            return True
        return False

    def goto_page(self, page: int) -> bool:
        if 1 <= page <= self.total_pages:
            self.current_offset = (page - 1) * self.page_size
            self.current_page = page
            self.last_accessed = time.time()
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "total_count": self.total_count,
            "page_size": self.page_size,
            "current_offset": self.current_offset,
            "current_page": self.current_page,
            "total_pages": self.total_pages,
            "has_next": self.current_offset + self.page_size < self.total_count,
            "has_prev": self.current_offset > 0,
        }


class PaginationManager:
    def __init__(self, max_sessions: int = 50, ttl_seconds: int = 1800):
        self._sessions: Dict[str, PaginationState] = {}
        self._max_sessions = max_sessions
        self._ttl = ttl_seconds

    def _cleanup(self):
        now = time.time()
        expired = [k for k, v in self._sessions.items() if now - v.last_accessed > self._ttl]
        for k in expired:
            del self._sessions[k]

    def create_session(
        self, session_id: str, base_url: str, total_count: int, page_size: int = 50
    ) -> Dict[str, Any]:
        self._cleanup()
        if len(self._sessions) >= self._max_sessions:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].last_accessed)
            del self._sessions[oldest]

        state = PaginationState(base_url, total_count, page_size)
        self._sessions[session_id] = state
        logger.info(f"Pagination session created: {session_id}, total={total_count}, pages={state.total_pages}")
        return state.to_dict()

    def get_session(self, session_id: str) -> Optional[PaginationState]:
        self._cleanup()
        return self._sessions.get(session_id)

    def next_page(self, session_id: str) -> Optional[Dict[str, Any]]:
        state = self.get_session(session_id)
        if state and state.next_page():
            return state.to_dict()
        return None

    def prev_page(self, session_id: str) -> Optional[Dict[str, Any]]:
        state = self.get_session(session_id)
        if state and state.prev_page():
            return state.to_dict()
        return None

    def goto_page(self, session_id: str, page: int) -> Optional[Dict[str, Any]]:
        state = self.get_session(session_id)
        if state and state.goto_page(page):
            return state.to_dict()
        return None

    def get_skip_top(self, session_id: str) -> Optional[tuple]:
        state = self.get_session(session_id)
        if state:
            return state.get_skip_top()
        return None

    def remove_session(self, session_id: str):
        self._sessions.pop(session_id, None)


pagination_manager = PaginationManager()
