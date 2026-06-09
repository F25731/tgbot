# 光鸭 Telegram 检索机器人

独立 Telegram 检索机器人服务，不依赖 AstrBot Telegram 平台。它通过现有光鸭资源 API 检索资源，并提供一个可视化后台网页调整运行配置。

## 功能

- 用户直接给 Telegram bot 发送关键词即可搜索
- `/gy 关键词` 搜索
- 搜索结果只以按钮显示资源名称
- 点击资源名称发送详情和链接
- 点击“下一页”继续翻页
- 后台网页配置每页显示数量
- 后台网页配置最多显示数量，`0` 表示不限
- Docker Compose 一键启动

## API 依赖

服务调用现有接口：

- `GET /api/external/search/health`
- `GET /api/external/search/resources?q=关键词&limit=10&cursor=...`
- `GET /api/external/search/resources/{resource_id}`

鉴权请求头：

```text
X-API-Key: 你的 search:read API Key
```

## 本地启动

```bash
cp .env.example .env
docker compose up -d --build
```

后台网页：

```text
http://服务器IP:8080
```

默认账号密码来自 `.env`：

```text
WEB_USERNAME=admin
WEB_PASSWORD=change-me
```

## 重要注意

同一个 Telegram bot token 只能有一个 polling 实例。不要把这个检索 bot token 同时配置到 AstrBot Telegram 平台里。

推荐结构：

- AstrBot 继续用推送机器人 token
- 本项目单独使用检索机器人 token

## 配置项

- `TELEGRAM_BOT_TOKEN`：检索机器人的 Telegram token
- `GUANGYA_API_BASE`：现有光鸭资源系统后端地址
- `GUANGYA_API_KEY`：具备 `search:read` 权限的 API Key
- `PAGE_SIZE`：每页显示数量，范围 1-50
- `MAX_RESULTS`：最多显示数量，`0` 表示不限
- `SEARCH_STATUS`：可选状态过滤，留空为全部
- `WEB_PORT`：后台网页端口

运行后也可以在后台网页修改配置。配置会保存到 `data/config.json`。
