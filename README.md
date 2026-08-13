# compose-webhook-action

GitHub Action：向本机 listener 发一次鉴权 POST，本机执行 `docker compose up --build -d`。

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

```toml
[[proxies]]
name = "compose-webhook"
type = "tcp"
localIP = "127.0.0.1"
localPort = 19090
remotePort = 19090
```

## 调用仓（zb）

仓库 secrets：

- `COMPOSE_WEBHOOK_URL` — `http://<frps>:19090/hook`
- `COMPOSE_WEBHOOK_TOKEN` — 与 listener 相同

```yaml
- uses: z-ph/compose-webhook-action@v1
  with:
    url: ${{ secrets.COMPOSE_WEBHOOK_URL }}
    token: ${{ secrets.COMPOSE_WEBHOOK_TOKEN }}
```

`wait: 'true'` 会轮询 `/status` 直到 compose 结束（默认只等 202）。

## 接口

| 方法 | 路径 | 鉴权 | 含义 |
| --- | --- | --- | --- |
| GET | `/health` | 否 | 存活 |
| POST | `/hook` | Bearer / `X-Webhook-Token` | 排队执行 compose，202 |
| GET | `/status` | 是 | 当前 job |

构建中再打 `/hook` → `409`。`WEBHOOK_DRY_RUN=1` 只记账。
