import asyncio
import json
from typing import Any

from redis import asyncio as aioredis
from redis.backoff import NoBackoff
from redis.retry import Retry

from config.env import RedisConfig, SchedulerConfig
from utils.log_util import logger


class SchedulerCommandClient:
    """只发送唤醒消息，不导入或启动调度运行时；持久化状态始终在业务事务中保存。"""

    @classmethod
    async def _notify(cls, payload: dict[str, Any]) -> bool:
        async def publish() -> None:
            timeout = SchedulerConfig.scheduler_notify_timeout_seconds
            redis = aioredis.Redis(
                host=RedisConfig.redis_host, port=RedisConfig.redis_port,
                username=RedisConfig.redis_username or None, password=RedisConfig.redis_password or None,
                db=RedisConfig.redis_database, socket_connect_timeout=timeout, socket_timeout=timeout,
                retry=Retry(NoBackoff(), 0),
            )
            try:
                await redis.publish(SchedulerConfig.sync_channel, json.dumps(payload))
            finally:
                await redis.aclose()

        try:
            await asyncio.wait_for(publish(), timeout=SchedulerConfig.scheduler_notify_timeout_seconds)
        except Exception:
            # 配置/执行请求已经提交，不能把通知故障伪装成保存失败。
            logger.warning('调度唤醒未送达，将由 Worker 周期扫描恢复')
            return False
        return True

    @classmethod
    async def request_scheduler_sync(cls, job_ids: set[int] | None = None) -> dict[str, Any]:
        notified = await cls._notify({'jobIds': sorted(job_ids) if job_ids is not None else None})
        return {
            'syncStatus': 'pending',
            'jobs': [{'jobId': job_id, 'syncStatus': 'pending'} for job_id in sorted(job_ids or [])],
            'notified': notified,
        }

    @classmethod
    async def request_execution_dispatch(cls) -> None:
        await cls._notify({'executions': True})