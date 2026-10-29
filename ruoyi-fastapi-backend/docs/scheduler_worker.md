# 独立调度 Worker

## 进程边界与启动

`ruoyi app run --env=dev` 只启动 Web 应用，不初始化、选主或关闭任务调度器。另开终端，在同一后端工作目录、同一虚拟环境和配置下执行 `ruoyi job worker --env=dev`，启动前台调度进程。生产环境使用进程管理器分别管理 Web 与 Worker；停止 Web 不等于停止 Worker。

`job worker` 先准备 CLI 环境配置，再延迟加载 Worker 运行时；命令注册及 `--help` 不导入调度器。它没有 `--output` 参数，也不通过普通短命令的日志静默策略运行。`run_worker()` 自行管理事件循环，不应从已有异步循环中再次包裹调用。

Worker 的数据库、Redis、任务调用代码和依赖必须与 Web 所用部署一致。Web 的应用租约与 Worker 的调度租约独立，互不代持。相同部署中只能有一个活跃调度 Worker；即使运行多个候选进程，也必须由调度租约约束唯一活跃者，不能仅依靠容器数量防止重复调度。

## 就绪前提

- 先完成数据库初始化/迁移，确保任务配置、同步状态与执行记录所需的表已经存在；Worker 不自动创建或升级表。
- 数据库和 Redis 可用、配置与部署一致，调度租约获取成功，且初次读取 SQL/同步完成，才可认为 Worker 可执行调度。进程存活、Web HTTP 就绪或请求已提交都不能单独证明这一点。
- Worker 不启动 FastAPI，不运行插件 HTTP 生命周期或依赖 Web 启动副作用。任务应直接使用明确配置的数据库、存储等服务，不依赖 Web 进程内状态。
- 文件类任务必须与 Web 访问同一存储。当前 `UploadConfig` 的上传、私有上传、回收站、核对隔离和下载目录均位于相对工作目录的 `vf_admin` 下；本地两个进程应在同一后端目录运行。

## 请求、通知与持久化语义

普通 `job run-once`、`job pause`、`job resume` 只在数据库会话内调用业务服务，不创建 Redis 调度上下文，不导入、初始化或关闭 `SchedulerManager`。`JobService` 负责在提交后通过 `SchedulerCommandClient` 发出通知。客户端只在通知时创建短生命周期异步 Redis 连接；不需要调用方绑定或关闭资源，通知异常和超时按最佳努力处理。

- 配置变更先保存任务及同步版本，再由 Worker 应用最新 SQL 状态。`saved` 只表示业务修改已提交；`syncStatus=pending` 不是已应用，须检查同步状态、错误及已应用版本。
- 手动执行先持久化请求并返回 `executionId` 和当前 `status`，由 Worker 派发。`pending` 或已提交并不表示实际开始，更不表示执行成功；最终结果应查询执行记录，任务删除后仍可使用执行 ID 追踪。
- `job sync` 只请求重新读取 SQL 配置，原样返回客户端的待处理结果；不提供本进程的 `isLeader`，也不等待应用完成。请求提交成功不保证 PubSub 已送达。
- Redis PubSub 是唤醒提示，不是可靠队列或事实来源。通知可能因断线、超时或订阅方离线丢失；SQL 中的任务配置、同步状态和执行请求才是事实来源。Worker 周期性配置同步与执行请求扫描补偿漏通知，Worker 停止期间的请求可以继续保持待处理。
- 已提交的修改不会因为通知失败而回滚。不要仅因 CLI 超时就反复提交 `run-once`，重复提交是不同的执行请求；先根据执行 ID 核对记录。
- 运行中执行可能因进程故障或强制退出而结果未知；不能据此推断业务副作用从未发生，也不应盲目自动重放。任务应自行具备幂等与核对能力。

## 配置

Web、CLI 与 Worker 应加载相同部署配置。以下为调度相关设置及默认计划值，秒数均以秒为单位：

| 环境变量 | 值 | 用途 |
| --- | --- | --- |
| `SCHEDULER_NOTIFY_TIMEOUT_SECONDS` | `1` | 短命令通知的超时上限 |
| `SCHEDULER_LEASE_SECONDS` | `60` | 调度租约有效期 |
| `SCHEDULER_RENEW_SECONDS` | `10` | 调度租约续期间隔 |
| `SCHEDULER_SHUTDOWN_SECONDS` | `15` | Worker 有界退出的等待预算 |
| `SCHEDULER_IO_TIMEOUT_SECONDS` | `5` | 调度基础设施 I/O 超时上限 |
| `SCHEDULER_SYNC_SECONDS` | `30` | 从 SQL 周期同步配置的间隔 |
| `SCHEDULER_DISPATCH_SECONDS` | `1` | 周期扫描和派发执行请求的间隔 |
| `SCHEDULER_STALL_SECONDS` | `30` | Worker 停滞 watchdog 的阈值 |
| `SCHEDULER_KEY_PREFIX` | `scheduler` | Redis 调度命名空间；不同部署必须使用不同前缀 |

不要跨独立部署共用调度键前缀，也不要将 Web 应用租约误当作调度租约。配置同步与派发间隔意味着即使通知丢失，持久化请求仍会在后续扫描时被观察，而不是承诺在固定时刻必定执行。

## 退出与 watchdog

正常停止应发送终止信号，让 Worker 先停止接收/派发新工作，再在有界预算内清理调度器、连接及租约。默认退出等待预算为 15 秒；对于退出过程阻塞或事件循环失去进展，独立 watchdog 必须限制进程拖延，不能无限等待线程、任务或基础设施 I/O。

外部进程管理器仍需提供最终强制退出上限。Compose 为 Worker 设置 `stop_grace_period: 25s`，给内部清理留出余量；超过外部宽限期仍未退出时将被强制停止。强制退出不等于业务任务被安全撤销，应结合 SQL 执行状态核对未知结果。

## VS Code 与 Compose

仓库根任务“MDM: 启动后端 Python 3.13”只启动 Web；另行运行“MDM: 启动调度 Worker Python 3.13”，两者使用同一 Python 3.13 环境的 `ruoyi` 入口、`--env=dev` 和后端工作目录，互不依赖启动。

根目录的 [PostgreSQL Compose](../../docker-compose.pg.yml) 与 [MySQL Compose](../../docker-compose.my.yml) 分别提供 `ruoyi-scheduler-pg`、`ruoyi-scheduler-my`。Worker 与对应 Web 使用相同 build/image 和环境，命令为 `ruoyi job worker --env=dockerpg` 或 `ruoyi job worker --env=dockermy`；不暴露 HTTP 端口，启用 `unless-stopped` 重启策略。

Compose 等待数据库和 Redis 的现有健康检查，但对后端只有 `service_started` 依赖：这不是后端 readiness，也不保证数据库表已建好。必须先按部署流程完成数据库初始化/迁移。Worker 因缺表等问题退出时，重启策略可以重试，但不能替代迁移或就绪验证。

两个容器均绑定宿主后端目录下的 `vf_admin` 到 `/app/vf_admin`；Dockerfile 的工作目录为 `/app`，与当前 `UploadConfig` 的相对路径一致。这同时覆盖文件任务涉及的上传、私有文件、回收站、隔离区和下载路径。已有容器可写层中的文件不会自动迁移到该宿主目录，切换前应备份并搬迁存量文件，确认容器用户权限；多宿主机部署需将此绑定替换为真正共享的存储。