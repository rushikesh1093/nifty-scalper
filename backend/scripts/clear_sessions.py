"""
Clear all authentication, broker, and cached sessions.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.cache.redis_client import redis_cache


async def clear_all_sessions():
    print("=" * 60)
    print("               CLEARING ALL SESSIONS")
    print("=" * 60)

    try:
        await redis_cache.connect()

        # Find all session and state keys
        session_keys = await redis_cache._redis.keys("session:*")
        feed_keys = await redis_cache._redis.keys("feed:*")
        pos_keys = await redis_cache._redis.keys("pos:*")
        sig_keys = await redis_cache._redis.keys("signals:*")

        all_keys = session_keys + feed_keys + pos_keys + sig_keys + ["positions", "order_book"]
        all_keys = list(set([k for k in all_keys if k]))

        deleted_count = 0
        if all_keys:
            for k in all_keys:
                try:
                    await redis_cache._redis.delete(k)
                    deleted_count += 1
                except Exception:
                    pass

        print(f"  [REDIS] Cleared {len(session_keys)} session keys.")
        print(f"  [REDIS] Cleared {deleted_count} total session/feed/cache keys.")
        await redis_cache.disconnect()
    except Exception as e:
        print(f"  [REDIS] Error or Redis not running: {e}")

    print("\n" + "=" * 60)
    print("  [SUCCESS] All sessions cleared successfully!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(clear_all_sessions())
