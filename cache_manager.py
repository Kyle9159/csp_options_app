"""
Cache Manager - Unified caching interface using SQLite database
Replaces fragmented JSON cache files with a single database-backed cache
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)

DB_PATH = Path("data/trading_bot.db")


class CacheManager:
    """Unified cache manager using SQLite database"""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._ensure_table_exists()

    def _ensure_table_exists(self):
        """Ensure cache_storage table exists"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cache_storage (
                    cache_key TEXT PRIMARY KEY,
                    cache_value TEXT NOT NULL,
                    data_type TEXT,
                    ttl_minutes INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cache_expires_at
                ON cache_storage(expires_at)
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error ensuring cache table exists: {e}")

    def get(self, key: str, default: Any = None) -> Optional[Any]:
        """
        Get value from cache with TTL check

        Args:
            key: Cache key
            default: Default value if not found or expired

        Returns:
            Cached value or default
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT cache_value, expires_at
                FROM cache_storage
                WHERE cache_key = ?
            """, (key,))

            result = cursor.fetchone()
            conn.close()

            if result:
                cache_value, expires_at = result
                expires_dt = datetime.fromisoformat(expires_at)

                # Check if expired
                if datetime.now() < expires_dt:
                    return json.loads(cache_value)
                else:
                    # Clean up expired entry
                    self.invalidate(key)
                    logger.debug(f"Cache expired for key: {key}")

            return default

        except Exception as e:
            logger.error(f"Error getting cache for {key}: {e}")
            return default

    def set(self, key: str, value: Any, ttl_minutes: int = 60, data_type: str = None) -> bool:
        """
        Set value in cache with TTL

        Args:
            key: Cache key
            value: Value to cache (must be JSON serializable)
            ttl_minutes: Time-to-live in minutes
            data_type: Optional data type classification

        Returns:
            True if successful
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            expires_at = datetime.now() + timedelta(minutes=ttl_minutes)
            cache_value = json.dumps(value)

            cursor.execute("""
                INSERT OR REPLACE INTO cache_storage
                (cache_key, cache_value, data_type, ttl_minutes, created_at, expires_at)
                VALUES (?, ?, ?, ?, datetime('now'), ?)
            """, (key, cache_value, data_type, ttl_minutes, expires_at.isoformat()))

            conn.commit()
            conn.close()
            logger.debug(f"Cached {key} with TTL {ttl_minutes} minutes")
            return True

        except Exception as e:
            logger.error(f"Error setting cache for {key}: {e}")
            return False

    def invalidate(self, key: str = None, pattern: str = None) -> int:
        """
        Invalidate cache entries

        Args:
            key: Single key to invalidate (exact match)
            pattern: Pattern to match (SQL LIKE pattern, e.g., "quotes_%")

        Returns:
            Number of entries deleted
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            if key:
                cursor.execute("DELETE FROM cache_storage WHERE cache_key = ?", (key,))
            elif pattern:
                cursor.execute("DELETE FROM cache_storage WHERE cache_key LIKE ?", (pattern,))
            else:
                # Clear all cache
                cursor.execute("DELETE FROM cache_storage")

            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            if deleted > 0:
                logger.info(f"Invalidated {deleted} cache entries")
            return deleted

        except Exception as e:
            logger.error(f"Error invalidating cache: {e}")
            return 0

    def cleanup_expired(self) -> int:
        """
        Remove all expired cache entries

        Returns:
            Number of entries deleted
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                DELETE FROM cache_storage
                WHERE expires_at < datetime('now')
            """)

            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            if deleted > 0:
                logger.info(f"Cleaned up {deleted} expired cache entries")
            return deleted

        except Exception as e:
            logger.error(f"Error cleaning up expired cache: {e}")
            return 0

    def get_stats(self) -> dict:
        """
        Get cache statistics

        Returns:
            Dictionary with cache stats
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Total entries
            cursor.execute("SELECT COUNT(*) FROM cache_storage")
            total = cursor.fetchone()[0]

            # Expired entries
            cursor.execute("""
                SELECT COUNT(*) FROM cache_storage
                WHERE expires_at < datetime('now')
            """)
            expired = cursor.fetchone()[0]

            # By data type
            cursor.execute("""
                SELECT data_type, COUNT(*)
                FROM cache_storage
                GROUP BY data_type
            """)
            by_type = dict(cursor.fetchall())

            conn.close()

            return {
                'total_entries': total,
                'expired_entries': expired,
                'active_entries': total - expired,
                'by_type': by_type
            }

        except Exception as e:
            logger.error(f"Error getting cache stats: {e}")
            return {
                'total_entries': 0,
                'expired_entries': 0,
                'active_entries': 0,
                'by_type': {}
            }

    def exists(self, key: str) -> bool:
        """
        Check if key exists and is not expired

        Args:
            key: Cache key

        Returns:
            True if exists and not expired
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT expires_at FROM cache_storage
                WHERE cache_key = ?
            """, (key,))

            result = cursor.fetchone()
            conn.close()

            if result:
                expires_dt = datetime.fromisoformat(result[0])
                return datetime.now() < expires_dt

            return False

        except Exception as e:
            logger.error(f"Error checking cache existence for {key}: {e}")
            return False


# Global cache manager instance
_cache_manager = None


def get_cache_manager() -> CacheManager:
    """Get or create global cache manager instance"""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = CacheManager()
    return _cache_manager


# Convenience functions
def cache_get(key: str, default: Any = None) -> Optional[Any]:
    """Convenience function to get from cache"""
    return get_cache_manager().get(key, default)


def cache_set(key: str, value: Any, ttl_minutes: int = 60, data_type: str = None) -> bool:
    """Convenience function to set cache"""
    return get_cache_manager().set(key, value, ttl_minutes, data_type)


def cache_invalidate(key: str = None, pattern: str = None) -> int:
    """Convenience function to invalidate cache"""
    return get_cache_manager().invalidate(key, pattern)


def cache_cleanup() -> int:
    """Convenience function to cleanup expired cache"""
    return get_cache_manager().cleanup_expired()


def cache_stats() -> dict:
    """Convenience function to get cache stats"""
    return get_cache_manager().get_stats()


if __name__ == "__main__":
    # Test the cache manager
    print("Testing Cache Manager...")

    cache = CacheManager()

    # Test set/get
    print("\n1. Testing set/get:")
    cache.set("test_key", {"value": 123, "name": "test"}, ttl_minutes=5)
    result = cache.get("test_key")
    print(f"   Stored and retrieved: {result}")

    # Test expiration
    print("\n2. Testing expiration:")
    cache.set("short_ttl", "expires soon", ttl_minutes=0)  # Already expired
    result = cache.get("short_ttl", default="EXPIRED")
    print(f"   Expired value: {result}")

    # Test stats
    print("\n3. Testing stats:")
    stats = cache.get_stats()
    print(f"   Cache stats: {stats}")

    # Test cleanup
    print("\n4. Testing cleanup:")
    deleted = cache.cleanup_expired()
    print(f"   Cleaned up {deleted} expired entries")

    # Test pattern invalidation
    print("\n5. Testing pattern invalidation:")
    cache.set("quotes_SPY", {"bid": 500}, ttl_minutes=10, data_type="quotes")
    cache.set("quotes_AAPL", {"bid": 180}, ttl_minutes=10, data_type="quotes")
    cache.set("grok_analysis", {"prob": 75}, ttl_minutes=10, data_type="grok")
    deleted = cache.invalidate(pattern="quotes_%")
    print(f"   Deleted {deleted} quote entries")

    print("\n✅ Cache Manager tests complete!")
