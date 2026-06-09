# 光鸭 Telegram 检索机器人

独立 Telegram 检索机器人服务，不依赖 AstrBot Telegram 平台。它通过现有光鸭资源 API 检索资源，并提供一个可视化后台网页调整运行配置。

## 功能

- 用户直接给 Telegram bot 发送关键词即可搜索
- `/gy 关键词` 搜索
- 搜索结果只以按钮显示资源名称
- 点击资源名称发送详情和链接
- 点击“下一页”继续翻页
- 后台网页配置 Telegram Bot Token
- 后台网页配置光鸭 API 地址和 API Key
- 后台网页配置每页显示数量
- 后台网页配置最多显示数量，`0` 表示不限
- Docker Compose 一键启动

## 部署

```bash
git clone https://github.com/F25731/tgbot.git
cd tgbot
cp .env.example .env
nano .env
docker compose up -d --build
```

`.env` 只需要先设置后台网页端口和登录账号：

```env
WEB_PORT=8080
WEB_USERNAME=admin
WEB_PASSWORD=change-me
```

后台网页：

```text
http://服务器IP:8080
```

登录后台后再配置：

- Telegram Bot Token
- 光鸭 API 地址
- 光鸭 API Key
- 每页显示数量
- 最多显示数量，`0` 表示不限
- 状态过滤

保存后点击“启动 Bot”或“重启 Bot”。

## 光鸭 API

服务调用现有接口：

- `GET /api/external/search/health`
- `GET /api/external/search/resources?q=关键词&limit=10&cursor=...`
- `GET /api/external/search/resources/{resource_id}`

鉴权请求头：

```text
X-API-Key: 你的 search:read API Key
```

如果本项目和光鸭后端在同一台 Docker 宿主机，但不在同一个 compose 网络里，可以在后台把 API 地址填成：

```text
http://host.docker.internal:你的后端端口
```

## 重要注意

同一个 Telegram bot token 只能有一个 polling 实例。不要把这个检索 bot token 同时配置到 AstrBot Telegram 平台里。

推荐结构：

- AstrBot 继续用推送机器人 token
- 本项目单独使用检索机器人 token

## 更新

```bash
cd /root/tgbot
git pull
docker compose up -d --build
```

配置会保存到 `data/config.json`，不会被 git 更新覆盖。
