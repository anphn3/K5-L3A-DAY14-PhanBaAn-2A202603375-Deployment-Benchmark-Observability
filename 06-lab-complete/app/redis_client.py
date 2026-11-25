"""
Kết nối Redis dùng chung cho rate limiter và cost guard.

  • Đọc REDIS_URL từ config
  • redis.from_url() không mở kết nối ngay → app vẫn khởi động được khi Redis chưa sẵn sàng
  • redis_healthy() dùng cho GET /ready: Redis không kết nối được → False
"""
import logging

import redis

from app.config import settings

logger = logging.getLogger(__name__)

# Timeout ngắn để /ready và /ask không treo lâu khi Redis chết
r = redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
    health_check_interval=30,
)


def redis_healthy() -> bool:
    try:
        return bool(r.ping())
    except redis.RedisError as e:
        logger.warning("redis_unhealthy", extra={"error": type(e).__name__})
        return False
