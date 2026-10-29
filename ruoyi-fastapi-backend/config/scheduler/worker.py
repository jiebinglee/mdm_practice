import asyncio
import signal
import threading

from redis import asyncio as aioredis
from redis.backoff import NoBackoff
from redis.retry import Retry

from config.database import DataSourceRegistry
from config.env import RedisConfig, SchedulerConfig
from config.scheduler.lease import terminate_worker
from config.scheduler.manager import SchedulerManager
from utils.log_util import logger


async def serve_worker(stop: asyncio.Event) -> None:
    """仅初始化任务所需资源；要求业务表已初始化，不启动 FastAPI/插件 HTTP 生命周期。"""
    redis = aioredis.Redis(
        host=RedisConfig.redis_host, port=RedisConfig.redis_port,
        username=RedisConfig.redis_username or None, password=RedisConfig.redis_password or None,
        db=RedisConfig.redis_database, decode_responses=True,
        socket_connect_timeout=SchedulerConfig.scheduler_io_timeout_seconds,
        socket_timeout=SchedulerConfig.scheduler_io_timeout_seconds,
        retry=Retry(NoBackoff(), 0),
    )

    async def pulse() -> None:
        while True:
            guard = SchedulerManager._lease_guard
            if guard is not None:
                guard.pulse()
            await asyncio.sleep(0.25)

    # 提前创建，启动全量同步卡死也会被守护线程检测。
    heartbeat = asyncio.create_task(pulse())
    try:
        await redis.ping()
        await DataSourceRegistry.initialize(log_enabled=False)
        await SchedulerManager.init_system_scheduler(redis)
        logger.info('独立 Scheduler Worker 已启动；调度身份与 Web 应用身份隔离')
        await stop.wait()
    finally:
        try:
            await SchedulerManager.close_system_scheduler()
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await redis.aclose()
            await DataSourceRegistry.dispose_all()
            await logger.complete()


def run_worker() -> None:
    """CLI 常驻入口；Windows spawn 子进程导入不会重复启动 Worker。"""
    timer: threading.Timer | None = None
    previous_handlers = {}

    async def main() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        task = asyncio.create_task(serve_worker(stop))

        def request_stop(signum: int, frame: object) -> None:
            nonlocal timer
            if timer is None:
                timer = threading.Timer(SchedulerConfig.scheduler_shutdown_seconds + 5, lambda: terminate_worker(1))
                timer.daemon = True
                timer.start()
            # 初始化可能尚未完成；取消也必须进入资源清理。
            loop.call_soon_threadsafe(stop.set)
            loop.call_soon_threadsafe(task.cancel)

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, request_stop)
        try:
            await task
        except asyncio.CancelledError:
            if not stop.is_set():
                raise

    try:
        asyncio.run(main())
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        if timer is not None:
            timer.cancel()


if __name__ == '__main__':
    run_worker()