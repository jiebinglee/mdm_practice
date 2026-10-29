# 本地 PostgreSQL 配置

开发环境的本机连接信息放在后端目录的 `.env.dev.local` 中。该文件已由根目录 `.gitignore` 排除，不要强制加入 Git 或分享其中的密码。

启动命令仍使用开发环境：`ruoyi app run --env=dev`。配置优先级为：外部环境变量 > `.env.dev.local` > `.env.dev`。本地文件只覆盖数据库，Redis 等配置继续沿用 `.env.dev`。

`DB_SOURCES` 中 PostgreSQL 数据源的 `db_ssl_mode` 支持 `disable`、`allow`、`prefer`、`require`、`verify-ca`、`verify-full`；未设置时保留驱动默认值。`require` 强制加密但不要求验证证书身份；需要验证服务器身份时应配置受信任 CA 并使用 `verify-full`。

同步驱动 psycopg2 使用 `sslmode` 参数，异步驱动 asyncpg 使用 `ssl` 参数。连接 URL 由 SQLAlchemy 结构化创建，密码中的 `@` 等字符无需手动转义。

当前本地文件沿用项目原有数据库名 `ruoyi-fastapi`。配置连接信息不会自动创建数据库、表或导入种子数据；连接检查应只执行只读查询。若远程使用其他数据库名，只修改本地文件中的 `db_database`。
