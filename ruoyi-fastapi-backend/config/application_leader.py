import asyncio
import random

from redis import asyncio as aioredis

from common.constant import LockConstant
from config.env import LogConfig
from utils.log_util import logger
from utils.server_util import StartupUtil, WorkerIdUtil


class ApplicationLeaderCoordinator:
    """Web 应用租约，不导入调度器；负责 OIDC 等应用级主节点职责。"""

    _redis: aioredis.Redis | None = None
    _is_leader = False
    _is_closing = False
    _renewal_task: asyncio.Task | None = None
    _reacquire_task: asyncio.Task | None = None

    @classmethod
    def get_application_lock_owner_token(cls) -> str:
        return StartupUtil.get_application_lock_owner_token(WorkerIdUtil.get_worker_id(LogConfig.log_worker_id))

    @classmethod
    def is_application_leader(cls) -> bool:
        return cls._is_leader

    @classmethod
    def start_application_lock_renewal(cls, redis: aioredis.Redis) -> asyncio.Task:
        cls._redis = redis
        cls._is_closing = False
        cls._is_leader = True
        if cls._renewal_task and not cls._renewal_task.done():
            return cls._renewal_task
        cls._renewal_task = StartupUtil.start_application_leader_renewal(
            redis=redis,
            lock_key=LockConstant.APP_STARTUP_LOCK_KEY,
            owner_token=cls.get_application_lock_owner_token(),
            lock_expire_seconds=LockConstant.LOCK_EXPIRE_SECONDS,
            interval_seconds=LockConstant.LOCK_RENEWAL_INTERVAL,
            on_lock_lost=cls.on_lock_lost,
        )
        return cls._renewal_task

    @classmethod
    async def init_application_leader(cls, redis: aioredis.Redis) -> None:
        cls._redis = redis
        cls._is_closing = False
        if cls._is_leader and cls._renewal_task and not cls._renewal_task.done():
            return
        if await StartupUtil.acquire_application_leader(
            redis, LockConstant.APP_STARTUP_LOCK_KEY,
            cls.get_application_lock_owner_token(), LockConstant.LOCK_EXPIRE_SECONDS,
        ):
            cls.start_application_lock_renewal(redis)
        else:
            cls._is_leader = False
            cls._ensure_reacquire()

    @classmethod
    def on_lock_lost(cls) -> None:
        cls._is_leader = False
        cls._ensure_reacquire()

    @classmethod
    def _ensure_reacquire(cls) -> None:
        if cls._is_closing or cls._redis is None:
            return
        if cls._reacquire_task is None or cls._reacquire_task.done():
            cls._reacquire_task = asyncio.create_task(cls._reacquire())

    @classmethod
    async def _reacquire(cls) -> None:
        try:
            while not cls._is_closing and not cls._is_leader:
                await asyncio.sleep(5 + random.uniform(0, 1))
                try:
                    await cls.init_application_leader(cls._redis)
                except Exception:
                    logger.exception('Application leader 租约重新竞争失败')
        finally:
            cls._reacquire_task = None

    @classmethod
    async def close(cls) -> None:
        cls._is_closing = True
        cls._is_leader = False
        for name in ('_renewal_task', '_reacquire_task'):
            task = getattr(cls, name)
            setattr(cls, name, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        redis, cls._redis = cls._redis, None
        if redis is not None:
            await StartupUtil.release_application_leader(
                redis, LockConstant.APP_STARTUP_LOCK_KEY, cls.get_application_lock_owner_token()
            )