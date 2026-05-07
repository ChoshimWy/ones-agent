"""指数退避重试装饰器"""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Callable, Sequence, Type

import structlog

log = structlog.get_logger()


def retry(
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    retry_on: Sequence[Type[Exception]] = (Exception,),
) -> Callable:
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except tuple(retry_on) as e:
                    last_exc = e
                    if attempt < max_retries:
                        wait = backoff_factor * (2 ** attempt)
                        log.warning("retry", func=func.__name__, attempt=attempt + 1, wait=wait, error=str(e))
                        time.sleep(wait)
                    else:
                        log.error("retry_exhausted", func=func.__name__, attempts=max_retries + 1, error=str(e))
            raise last_exc
        return wrapper
    return decorator


def aretry(
    max_retries: int = 3,
    backoff_factor: float = 1.0,
    retry_on: Sequence[Type[Exception]] = (Exception,),
) -> Callable:
    """Async version of retry decorator."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except tuple(retry_on) as e:
                    last_exc = e
                    if attempt < max_retries:
                        wait = backoff_factor * (2 ** attempt)
                        log.warning("aretry", func=func.__name__, attempt=attempt + 1, wait=wait, error=str(e))
                        await asyncio.sleep(wait)
                    else:
                        log.error("aretry_exhausted", func=func.__name__, attempts=max_retries + 1, error=str(e))
            raise last_exc
        return wrapper
    return decorator
