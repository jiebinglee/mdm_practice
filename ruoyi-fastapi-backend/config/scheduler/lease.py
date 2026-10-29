import json
import multiprocessing
import os
import threading
import time
from collections.abc import Callable

from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from config.env import RedisConfig, SchedulerConfig
from utils.server_util import StartupUtil


def lease_redis() -> Redis:
    """每个进程/守护线程独立创建有界同步客户端，不共享异步连接。"""
    timeout = SchedulerConfig.scheduler_io_timeout_seconds
    return Redis(
        host=RedisConfig.redis_host, port=RedisConfig.redis_port,
        username=RedisConfig.redis_username or None, password=RedisConfig.redis_password or None,
        db=RedisConfig.redis_database, decode_responses=True,
        socket_connect_timeout=timeout, socket_timeout=timeout,
        retry=Retry(NoBackoff(), 0),
    )


def owns_scheduler_lease(owner: str) -> bool:
    """在任务真实调用前再次验证调度代次，包括进程池中尚未开始的任务。"""
    with lease_redis() as redis:
        return redis.get(SchedulerConfig.lease_key) == owner


def terminate_worker(exit_code: int) -> None:
    """故障截止时同步终止本 Worker 的进程池，避免 Windows 遗留子进程。"""
    for child in multiprocessing.active_children():
        try:
            child.terminate()
        except (OSError, ValueError):
            pass
    os._exit(exit_code)


class SchedulerLeaseGuard:
    """独立于调度事件循环续租；失锁或循环卡死时 fail-closed 退出整个 Worker。"""

    def __init__(self, owner: str, *, fatal: Callable[[int], None] = terminate_worker) -> None:
        self.owner = owner
        self.valid = True
        self.ready = False
        self._fatal = fatal
        self._stop = threading.Event()
        self._pulse = time.monotonic()
        self._thread = threading.Thread(target=self._run, name='scheduler-lease-guard', daemon=True)

    def pulse(self) -> None:
        self._pulse = time.monotonic()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.valid = False
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=SchedulerConfig.scheduler_io_timeout_seconds + 1)

    def revoke(self) -> None:
        self.valid = False
        # 不能把终止只排进可能卡死的事件循环；不释放租约，等待 TTL 后才能接管。
        self._fatal(1)

    def _renew(self, redis: Redis) -> bool:
        renewed = redis.eval(
            StartupUtil._RENEW_SCRIPT, 1, SchedulerConfig.lease_key,
            self.owner, SchedulerConfig.scheduler_lease_seconds,
        )
        if not renewed:
            return False
        # 健康状态只报告本进程最近成功续期，不作为可靠命令存储。
        redis.set(
            SchedulerConfig.health_key,
            json.dumps({
                'pid': os.getpid(), 'owner': self.owner,
                'status': 'leader' if self.ready else 'starting',
            }),
            ex=SchedulerConfig.scheduler_lease_seconds,
        )
        return True

    def _run(self) -> None:
        next_renew = 0.0
        try:
            with lease_redis() as redis:
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now - self._pulse >= SchedulerConfig.scheduler_stall_seconds:
                        self.revoke()
                        return
                    if now >= next_renew:
                        if not self._renew(redis):
                            self.revoke()
                            return
                        next_renew = time.monotonic() + SchedulerConfig.scheduler_renew_seconds
                    self._stop.wait(0.25)
        except Exception:
            if not self._stop.is_set():
                self.revoke()