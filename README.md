# compose-webhook-action

GitHub Action：先 `GET /health` 确认 listener 存活，再发一次鉴权 POST，本机执行 `docker compose up --build -d`。

listener 只绑 `127.0.0.1`。外网用 FRP 转发。不把 docker.sock 挂进容器。

## 本机 listener

```powershell
cd D:\git\compose-webhook-action
copy listener\.env.example listener\.env
# 改 WEBHOOK_TOKEN / COMPOSE_WORKDIR
python listener\server.py
```

`listener/.env` 示例（对应 `2024-shiliuzi/zb`）：

```
WEBHOOK_TOKEN=至少16位随机串
WEBHOOK_HOST=127.0.0.1
WEBHOOK_PORT=19090
COMPOSE_WORKDIR=D:/git/zb
COMPOSE_FILE=docker/docker-compose.yml
COMPOSE_ENV=docker/.env
COMPOSE_ARGS=up --build -d
```

### FRP

TCP 隧道对端永远是 `127.0.0.1`。要记真实源 IP，**只给 webhook 这一条**开 PROXY protocol（先重启 listener，再重载 frpc）：

```toml
[[proxies]]
name = "tpp-webhook"
type = "tcp"
localIP = "127.0.0.1"
localPort = 19090
remotePort = 19090
transport.proxyProtocolVersion = "v2"
```

不要给 `ssh-home` / `caigou` 开。listener 会剥掉 PROXY 头，日志变成 `203.0.113.9 xff=- - "GET /info.php ..."`。

不要改成 `type = "http"`：要占 frps vhost HTTP 口，和现在的 `http://<ip>:19090/hook` 不兼容。

## 调用仓（zb）

仓库 secrets：

- `COMPOSE_WEBHOOK_URL` — `https://webhook.example/hook`（可省略 `/hook`；缺 scheme 时域名默认 `https://`，`host:port` / IP 默认 `http://`）
- `COMPOSE_WEBHOOK_TOKEN` — 与 listener 相同

```yaml
- uses: z-ph/compose-webhook-action@v1
  with:
    url: ${{ secrets.COMPOSE_WEBHOOK_URL }}
    token: ${{ secrets.COMPOSE_WEBHOOK_TOKEN }}
```

触发顺序：`GET /health`（200 才继续）→ `POST /hook`。`/health` 失败则不打 `/hook`。

`wait: 'true'` 会轮询 `/status` 直到 compose 结束（默认只等 202）。

## 接口

| 方法 | 路径 | 鉴权 | 含义 |
| --- | --- | --- | --- |
| GET | `/health` | 否 | 存活 |
| POST | `/hook` | Bearer / `X-Webhook-Token` | 排队执行 compose，202 |
| GET | `/status` | 是 | 当前 job |

构建中再打 `/hook` → `409`。`WEBHOOK_DRY_RUN=1` 只记账。
