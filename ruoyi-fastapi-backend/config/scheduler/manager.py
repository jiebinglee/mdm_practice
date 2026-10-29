import asyncio
import json
import os
import random
import time
from datetime import timezone
from typing import Any
from uuid import uuid4

from apscheduler.events import EVENT_ALL, SchedulerEvent
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis import asyncio as aioredis

import module_task  # noqa: F401
from config.env import LogConfig, SchedulerConfig
from config.scheduler.dispatcher import SchedulerDispatcher
from config.scheduler.jobs import SchedulerJobs
from config.scheduler.lease import SchedulerLeaseGuard
from config.scheduler.listener import SchedulerJobListener
from config.scheduler.resources import SchedulerResources
from config.scheduler.synchronization import SchedulerSynchronizer
from module_admin.entity.vo.job_vo import JobModel
from utils.log_util import logger
from utils.server_util import StartupUtil, WorkerIdUtil

scheduler = AsyncIOScheduler(timezone=timezone.utc)


class SchedulerManager:
    """
    定时任务统一入口，负责生命周期、Leader控制和跨进程同步通知
    """

    _scheduler: AsyncIOScheduler = scheduler
    _is_leader: bool = False
    _worker_id: str = WorkerIdUtil.get_worker_id(LogConfig.log_worker_id)
    _scheduler_lock_owner_token: str = f'{os.getpid()}:{uuid4().hex}'
    _lease_guard: SchedulerLeaseGuard | None = None
    _redis: aioredis.Redis | None = None
    _sync_channel: str = SchedulerConfig.sync_channel
    _sync_listener_task: asyncio.Task | None = None
    _lock_lost_task: asyncio.Task | None = None
    _sync_task: asyncio.Task | None = None
    _sync_pending: bool = False
    _sync_full_pending: bool = False
    _sync_pending_ids: set[int] = set()
    _sync_lock: asyncio.Lock = asyncio.Lock()
    _last_sync_at: float | None = None
    _sync_debounce_seconds: float = 0.5
    _sync_min_interval_seconds: float = 2.0
    _reacquire_task: asyncio.Task | None = None
    _reacquire_interval_seconds: float = 5.0
    _reacquire_jitter_seconds: float = 1.0
    _is_closing: bool = False

    _resources = SchedulerResources()
    _jobs = SchedulerJobs(scheduler)
    _synchronizer = SchedulerSynchronizer(_jobs, _resources)
    _dispatcher = SchedulerDispatcher(_jobs, _resources, _synchronizer)
    _listener = SchedulerJobListener(_resources)

    @classmethod
    def _should_enable_scheduler_sync(cls) -> bool:
        """
        判断是否需要启用多 worker 的任务状态同步机制

        :return: 是否开启定时同步与监听
        """
        return True

    @classmethod
    async def init_system_scheduler(cls, redis: aioredis.Redis) -> None:
        """
        应用启动时初始化定时任务（使用分布式锁确保只有一个worker启动scheduler）

        :param redis: Redis连接对象
        :return: None
        """
        cls._redis = redis
        cls._is_closing = False
        logger.debug(f'🔎 Worker {cls._worker_id} 尝试获取独立 Scheduler 锁...')

        acquired = await StartupUtil.acquire_application_leader(
            redis=redis,
            lock_key=SchedulerConfig.lease_key,
            owner_token=cls.get_scheduler_lock_owner_token(),
            lock_expire_seconds=SchedulerConfig.scheduler_lease_seconds,
        )

        if acquired:
            await cls._activate_scheduler_as_leader(redis)
        else:
            cls._is_leader = False
            logger.debug(f'⏸️ Worker {cls._worker_id} 未持有 Scheduler 锁，等待接管')
            cls._ensure_reacquire_task()

    @classmethod
    def get_scheduler_lock_owner_token(cls) -> str:
        """
        获取当前调度进程独有的租约 token，不复用 Web 应用租约。

        :return: Application锁owner token
        """
        return cls._scheduler_lock_owner_token

    @classmethod
    def is_scheduler_leader(cls) -> bool:
        """
        判断当前进程是否仍以Application leader身份运行。

        :return: 是否为Application leader
        """
        return cls._is_leader and (cls._lease_guard is None or cls._lease_guard.valid)

    @classmethod
    def start_scheduler_lock_renewal(cls, redis: aioredis.Redis) -> None:
        """同步存储可能阻塞调度循环，续租和停顿检测必须在独立线程。"""
        cls._redis = redis
        if cls._lease_guard is None:
            cls._lease_guard = SchedulerLeaseGuard(cls.get_scheduler_lock_owner_token())
            cls._lease_guard.start()

    @classmethod
    async def stop_scheduler_lock_renewal(cls) -> None:
        guard, cls._lease_guard = cls._lease_guard, None
        if guard is not None:
            await asyncio.to_thread(guard.stop)

    @classmethod
    async def _start_scheduler_as_leader(cls, redis: aioredis.Redis) -> None:
        """
        以 Leader 身份启动 Scheduler（内部方法，调用前需确保已持有锁）

        :param redis: Redis连接对象
        :return: None
        """
        cls._is_leader = True
        cls._scheduler._worker_owner_token = cls.get_scheduler_lock_owner_token()
        cls._scheduler._worker_can_submit = cls.is_scheduler_leader
        logger.info(f'🎯 Worker {cls._worker_id} 持有 Scheduler 锁，开始启动定时任务...')
        # 懒加载配置 scheduler
        cls._configure_scheduler()
        cls._scheduler.start(paused=True)
        cls._jobs.update_time_cache.clear()
        cls._synchronizer.applied_jobs.clear()
        cls._sync_pending_ids = set()
        cls._sync_full_pending = False
        # 先清除持久化 job store 中停用/删除的任务，再允许执行。
        await cls._sync_jobs_from_database(raise_errors=True)
        try:
            cls._scheduler.remove_listener(cls.scheduler_event_listener)
        except ValueError:
            pass
        cls._scheduler.add_listener(cls.scheduler_event_listener, EVENT_ALL)
        cls._scheduler.add_job(
            func=cls.request_scheduler_sync,
            trigger='interval',
            seconds=SchedulerConfig.scheduler_sync_seconds,
            id='_scheduler_job_sync',
            name='Scheduler任务同步',
            replace_existing=True,
        )
        cls._scheduler.add_job(
            func=cls._drain_execution_requests,
            trigger='interval',
            seconds=SchedulerConfig.scheduler_dispatch_seconds,
            id='_scheduler_execution_dispatch',
            name='Scheduler执行请求派发',
            replace_existing=True,
        )
        if cls._should_enable_scheduler_sync():
            cls._sync_listener_task = asyncio.create_task(cls._listen_sync_channel(redis))
        cls._scheduler.resume()
        if cls._lease_guard is not None:
            cls._lease_guard.ready = True

        logger.info('✅ 系统初始定时任务加载成功')

    @classmethod
    async def _activate_scheduler_as_leader(cls, redis: aioredis.Redis) -> None:
        """
        启动租约续期并以Application leader身份激活Scheduler。

        Scheduler启动失败时立即停止续期并原子释放租约，避免故障worker继续占用
        Application leader身份。

        :param redis: Redis连接对象
        :return: None
        """
        cls.start_scheduler_lock_renewal(redis)
        try:
            await cls._start_scheduler_as_leader(redis)
        except BaseException:
            cls._is_leader = False
            if getattr(cls._scheduler, 'running', False):
                cls._scheduler.shutdown(wait=False)
                await asyncio.sleep(0)
            cls._resources.dispose()
            await cls.stop_scheduler_lock_renewal()
            try:
                await StartupUtil.release_application_leader(
                    redis,
                    SchedulerConfig.lease_key,
                    cls.get_scheduler_lock_owner_token(),
                )
            except Exception:
                logger.exception('❌ Scheduler 启动失败后释放调度租约失败')
            raise

    @classmethod
    def on_lock_lost(cls) -> None:
        """
        锁丢失处理入口

        :return: None
        """
        if not cls._is_leader:
            return
        cls._is_leader = False
        logger.warning(f'⚠️ Worker {cls._worker_id} 失去 Scheduler 锁')
        if cls._lock_lost_task:
            cls._lock_lost_task.cancel()
        cls._lock_lost_task = asyncio.create_task(cls._handle_lock_lost())

    @classmethod
    async def _handle_lock_lost(cls) -> None:
        """
        处理锁丢失后的资源释放

        :return: None
        """
        if cls._sync_listener_task:
            cls._sync_listener_task.cancel()
            try:
                await cls._sync_listener_task
            except asyncio.CancelledError:
                pass
            cls._sync_listener_task = None
        if cls._sync_task:
            cls._sync_task.cancel()
            try:
                await cls._sync_task
            except asyncio.CancelledError:
                pass
            cls._sync_task = None
            cls._sync_pending = False
        if getattr(cls._scheduler, 'running', False):
            cls._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
        cls._resources.dispose()
        await cls.stop_scheduler_lock_renewal()
        cls._ensure_reacquire_task()

    @classmethod
    def _ensure_reacquire_task(cls) -> None:
        """
        启动锁重新竞争任务

        :return: None
        """
        if cls._is_closing or not cls._redis:
            return
        if cls._reacquire_task and not cls._reacquire_task.done():
            return
        cls._reacquire_task = asyncio.create_task(cls._run_reacquire_loop())

    @classmethod
    def _get_reacquire_delay(cls) -> float:
        """
        获取带随机抖动的锁重新竞争间隔

        :return: 重新竞争等待秒数
        """
        return cls._reacquire_interval_seconds + random.uniform(0, cls._reacquire_jitter_seconds)

    @classmethod
    async def _run_reacquire_loop(cls) -> None:
        """
        循环尝试重新获取锁并恢复调度器

        :return: None
        """
        try:
            while not cls._is_leader and not cls._is_closing:
                await asyncio.sleep(cls._get_reacquire_delay())
                if cls._is_closing:
                    break
                if not cls._redis:
                    continue
                try:
                    acquired = await StartupUtil.acquire_application_leader(
                        redis=cls._redis,
                        lock_key=SchedulerConfig.lease_key,
                        owner_token=cls.get_scheduler_lock_owner_token(),
                        lock_expire_seconds=SchedulerConfig.scheduler_lease_seconds,
                    )
                except Exception as exc:
                    logger.error(f'❌ Scheduler 租约重新竞争失败：{exc}')
                    continue
                if acquired:
                    try:
                        await cls._activate_scheduler_as_leader(cls._redis)
                    except Exception:
                        logger.exception('❌ 重新获得调度租约后恢复 Scheduler 失败')
                        continue
                    return
        except asyncio.CancelledError:
            raise
        finally:
            cls._reacquire_task = None

    @classmethod
    async def close_system_scheduler(cls) -> None:
        """
        应用关闭时关闭定时任务

        :return: None
        """
        cls._is_closing = True
        cls._is_leader = False
        if cls._sync_listener_task:
            cls._sync_listener_task.cancel()
            try:
                await cls._sync_listener_task
            except asyncio.CancelledError:
                pass
            cls._sync_listener_task = None
        if cls._sync_task:
            cls._sync_task.cancel()
            try:
                await cls._sync_task
            except asyncio.CancelledError:
                pass
            cls._sync_task = None
            cls._sync_pending = False
        if cls._reacquire_task:
            cls._reacquire_task.cancel()
            try:
                await cls._reacquire_task
            except asyncio.CancelledError:
                pass
            cls._reacquire_task = None
        if cls._lock_lost_task:
            cls._lock_lost_task.cancel()
            try:
                await cls._lock_lost_task
            except asyncio.CancelledError:
                pass
            cls._lock_lost_task = None
        if getattr(cls._scheduler, 'running', False):
            cls._scheduler.pause()
            # 先等待在途任务；不以 shutdown(wait=True) 阻塞当前事件循环。
            pending = set()
            for executor in cls._scheduler._executors.values():
                for future in tuple(getattr(executor, '_pending_futures', ())):
                    pending.add(future if asyncio.isfuture(future) else asyncio.wrap_future(future))
            if pending:
                await asyncio.wait(pending, timeout=SchedulerConfig.scheduler_shutdown_seconds)
            cls._scheduler.shutdown(wait=False)
            await asyncio.sleep(0)
            logger.info('⏹️ 关闭定时任务成功')
        cls._resources.dispose()
        await cls.stop_scheduler_lock_renewal()
        # 必须在 Redis 连接池关闭前，仅释放自己的调度租约。
        redis = cls._redis
        cls._redis = None
        try:
            if redis:
                released = await StartupUtil.release_application_leader(
                    redis,
                    SchedulerConfig.lease_key,
                    cls.get_scheduler_lock_owner_token(),
                )
                if released:
                    logger.info(f'🔓 Worker {cls._worker_id} 释放 Scheduler 锁')
        finally:
            cls._is_leader = False

    @classmethod
    async def request_scheduler_sync(
        cls,
        job_ids: set[int] | None = None,
        *,
        immediate: bool = False,
    ) -> dict[str, Any]:
        """
        请求调度器同步任务状态

        :param job_ids: 指定受影响任务，None 表示完整校准
        :param immediate: Leader 是否在返回前完成本地同步
        :return: 已应用、待同步或失败的明确结果
        """
        if cls._is_leader and immediate:
            async with cls._sync_lock:
                # 等待同步锁期间可能失去租约，此时交由新 Leader 处理。
                if cls._is_leader:
                    result = await cls._sync_jobs_from_database(job_ids)
                    if cls._is_leader:
                        cls._last_sync_at = time.monotonic()
                        return result
        if cls._is_leader:
            cls._sync_pending = True
            if job_ids is None:
                cls._sync_full_pending = True
            else:
                cls._sync_pending_ids.update(job_ids)
            cls._ensure_sync_task()
        elif cls._redis:
            try:
                await cls._redis.publish(
                    cls._sync_channel, json.dumps({'jobIds': sorted(job_ids) if job_ids is not None else None})
                )
            except Exception:
                # 数据库已提交，Leader 的周期同步负责重试。
                logger.exception('❌ 调度同步通知失败，将由 Leader 周期同步重试')
        return SchedulerSynchronizer.result(
            [{'jobId': job_id, 'syncStatus': 'pending'} for job_id in sorted(job_ids or [])], default='pending'
        )

    @classmethod
    def _ensure_sync_task(cls) -> None:
        """
        启动同步调度任务

        :return: None
        """
        if cls._sync_task and not cls._sync_task.done():
            return
        cls._sync_task = asyncio.create_task(cls._run_sync_loop())

    @classmethod
    async def _run_sync_loop(cls) -> None:
        """
        执行同步调度循环

        :return: None
        """
        try:
            while True:
                if not cls._sync_pending:
                    break
                await asyncio.sleep(cls._sync_debounce_seconds)
                await cls._sync_with_throttle()
        except asyncio.CancelledError:
            raise
        finally:
            cls._sync_task = None

    @classmethod
    async def _sync_with_throttle(cls) -> None:
        """
        按节流规则执行同步

        :return: None
        """
        async with cls._sync_lock:
            if not cls._is_leader:
                return
            if cls._last_sync_at:
                elapsed = time.monotonic() - cls._last_sync_at
                if elapsed < cls._sync_min_interval_seconds:
                    await asyncio.sleep(cls._sync_min_interval_seconds - elapsed)
            if not cls._is_leader:
                return
            job_ids = None if cls._sync_full_pending else set(cls._sync_pending_ids)
            cls._sync_pending = False
            cls._sync_full_pending = False
            cls._sync_pending_ids.clear()
            await cls._sync_jobs_from_database(job_ids)
            cls._last_sync_at = time.monotonic()

    @classmethod
    async def _listen_sync_channel(cls, redis: aioredis.Redis) -> None:
        """
        监听同步请求通道

        :param redis: Redis连接对象
        :return: None
        """
        while True:
            pubsub = redis.pubsub()
            try:
                await pubsub.subscribe(cls._sync_channel)
                async for message in pubsub.listen():
                    if not cls._is_leader:
                        continue
                    if message.get('type') != 'message':
                        continue
                    await cls._handle_sync_notification(message.get('data', '{}'))
            except asyncio.CancelledError:
                await pubsub.unsubscribe(cls._sync_channel)
                await pubsub.close()
                raise
            except Exception as e:
                logger.error(f'❌ Scheduler 同步监听异常：{e}，5 秒后重试...')
                await pubsub.close()
                await asyncio.sleep(5)
            finally:
                try:
                    await pubsub.close()
                except Exception:
                    pass

    @classmethod
    async def _handle_sync_notification(cls, data: str | bytes) -> None:
        """
        解析唤醒通知；实际配置和执行请求均重新从数据库读取

        :param data: 同步通知数据
        :return: None
        """
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict):
                return
            if payload.get('executions'):
                await cls._drain_execution_requests()
            if 'jobIds' in payload:
                ids = payload['jobIds']
                if ids is None or (isinstance(ids, list) and all(type(item) is int for item in ids)):
                    await cls.request_scheduler_sync(set(ids) if ids is not None else None)
        except (TypeError, ValueError):
            logger.warning('⚠️ 忽略无效的任务同步通知')

    @classmethod
    async def request_execution_dispatch(cls) -> None:
        """
        唤醒 Leader 处理持久化执行请求，通知失败由周期派发恢复

        :return: None
        """
        try:
            if cls._is_leader:
                await cls._drain_execution_requests()
            elif cls._redis:
                await cls._redis.publish(cls._sync_channel, json.dumps({'executions': True}))
        except Exception:
            logger.exception('❌ 执行请求已保存，唤醒派发失败，将由周期派发重试')

    @classmethod
    def _configure_scheduler(cls) -> None:
        """
        配置当前调度器的任务存储、执行器和事件回调

        :return: None
        """
        cls._resources.configure(cls._scheduler, cls.scheduler_event_listener)

    @classmethod
    async def _sync_jobs_from_database(
        cls, job_ids: set[int] | None = None, *, raise_errors: bool = False
    ) -> dict[str, Any]:
        """
        调用配置同步功能类校准数据库任务

        :param job_ids: 指定任务ID，None表示完整校准
        :param raise_errors: 是否向调用方抛出完整扫描异常
        :return: 配置应用结果
        """
        return await cls._synchronizer.sync_jobs(
            job_ids, is_leader=cls.is_scheduler_leader, raise_errors=raise_errors
        )

    @classmethod
    async def _drain_execution_requests(cls) -> None:
        """
        调用执行派发功能类处理已持久化的执行请求

        :return: None
        """
        await cls._dispatcher.dispatch_pending(is_leader=cls.is_scheduler_leader)

    @classmethod
    def scheduler_event_listener(cls, event: SchedulerEvent) -> None:
        """
        调用日志功能类记录任务执行事件

        :param event: 调度器产生的执行事件
        :return: None
        """
        cls._listener.handle_event(event)

    @staticmethod
    def _prepare_scheduler_job_add(job_info: JobModel) -> dict[str, Any]:
        """
        校验任务配置并构造注册参数

        :param job_info: 任务配置
        :return: 调度器任务注册参数
        """
        return SchedulerJobs.prepare_job(job_info)

    @classmethod
    def get_scheduler_job(cls, job_id: str | int) -> Job | None:
        """
        获取当前进程的调度任务

        :param job_id: 任务ID
        :return: 调度任务，不存在时返回None
        """
        return cls._jobs.get_job(job_id)

    @classmethod
    def add_scheduler_job(cls, job_info: JobModel) -> None:
        """
        在Leader进程中注册任务

        :param job_info: 任务配置
        :return: None
        """
        if cls._is_leader:
            cls._jobs.add_job(job_info)

    @classmethod
    def remove_scheduler_job(cls, job_id: str | int) -> None:
        """
        在Leader进程中移除任务

        :param job_id: 任务ID
        :return: None
        """
        if cls._is_leader:
            cls._jobs.remove_job(job_id)

    @classmethod
    def execute_scheduler_job_once(cls, job_info: JobModel, *, execution_id: str, dispatch_token: str) -> None:
        """
        在Leader进程中注册已领取的单次执行请求

        :param job_info: 任务配置
        :param execution_id: 已持久化的执行请求ID
        :param dispatch_token: 本次派发的领取凭据
        :return: None
        """
        if not cls._is_leader:
            raise RuntimeError('仅 Leader 可以派发已领取的执行请求')
        cls._jobs.execute_once(job_info, execution_id=execution_id, dispatch_token=dispatch_token)
