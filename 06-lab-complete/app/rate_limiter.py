"""
Rate limiting (tiêu chí 7).

  • Mỗi API key tối đa RATE_LIMIT_PER_MINUTE request trong 60 giây (sliding window)
  • Vượt giới hạn → HTTP 429 kèm header Retry-After
  • State lưu trong Redis (sorted set) → mọi worker / instance dùng chung, còn nguyên sau restart
"""
import hashlib
import math
import time
import uuid

import redis
from fastapi import HTTPException

from app.config import settings
from app.redis_client import r

WINDOW_SECONDS = 60


def _bucket(key: str) -> str:
    # Không lưu API key thô trong Redis
    return "ratelimit:" + hashlib.sha256(key.encode()).hexdigest()[:32]


def check_rate_limit(key: str) -> None:
    limit = settings.rate_limit_per_minute
    bucket = _bucket(key)
    now = time.time()
    member = f"{now:.6f}-{uuid.uuid4().hex[:8]}"

    try:
        # MULTI/EXEC: các worker không chen ngang nhau giữa lúc đếm và thêm
        pipe = r.pipeline(transaction=True)
        pipe.zremrangebyscore(bucket, 0, now - WINDOW_SECONDS)
        pipe.zadd(bucket, {member: now})
        pipe.zcard(bucket)
        pipe.expire(bucket, WINDOW_SECONDS + 1)
        _, _, count, _ = pipe.execute()

        if count > limit:
            # Không tính request bị chặn vào cửa sổ
            r.zrem(bucket, member)
            oldest = r.zrange(bucket, 0, 0, withscores=True)
            retry_after = WINDOW_SECONDS
            if oldest:
                retry_after = max(1, math.ceil(oldest[0][1] + WINDOW_SECONDS - now))
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {limit} requests per {WINDOW_SECONDS}s",
                headers={"Retry-After": str(retry_after)},
            )
    except redis.RedisError:
        # Không có Redis thì không đếm được → từ chối thay vì cho qua không giới hạn
        raise HTTPException(status_code=503, detail="Rate limiter unavailable")
