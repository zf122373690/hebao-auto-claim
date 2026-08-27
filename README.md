# 和包自动领取 Cloudflare Worker

基于 Cloudflare Workers、D1 和 Durable Objects 的和包自动领取服务。

## 安装与部署

需要 Node.js 18+ 和 Cloudflare 账号：

```powershell
npm install
npx wrangler login
npx wrangler d1 execute hebao --remote --file=schema.sql
npm run check
npx wrangler deploy
```

项目已配置 D1 数据库 `hebao`，ID 为 `15732dfc-18e3-4696-b1b3-f883e0a32fab`。首次使用必须在 Cloudflare D1 Console 执行 `schema.sql`，或使用上面的 `--remote` 命令。必须初始化远程数据库。

部署后访问 `https://hebao.1992418.xyz`，健康检查为 `/healthz`。发布前可执行 `npx wrangler deploy --dry-run`。

## 短信 Webhook

T3 Lite 选择自定义 Webhook，地址填写：

```text
https://你的域名/api/hooks
```

推荐 JSON：

```json
{"type":"sms","message":"您的验证码为123456","simNumber":"15901357458"}
```

正文支持 `message`、`text`、`content`、`body`，手机号支持 `simNumber`、`phone`。Token 可放在 `Authorization: Bearer <Token>`、`X-Device-Token` 或 `x-webhook-secret`。若配置了 `WEBHOOK_SECRET`，Lite Token 必须一致。

## 使用网页

添加账号后勾选商品并启动。首次登录成功后，`sess_key` 保存到 D1，后续任务复用，会话失效后才重新登录。页面会显示任务日志；商品验证码超过 75 秒未收到会自动重发，多个商品之间遵守 60 秒频控。

## 每日定时

网页“每日定时执行”支持设置北京时间、启停、账号、商品和每个商品次数，配置保存到 D1 的 `settings` 表。Worker 每 5 分钟触发 Cron，只在设定时间执行，并用 `lastRun` 防止当天重复执行。

## 常用命令

```powershell
npm run dev
npm run check
npx wrangler d1 execute hebao --remote --command "SELECT * FROM accounts"
npx wrangler d1 execute hebao --remote --command "SELECT * FROM sms_messages ORDER BY received_at DESC LIMIT 20"
npx wrangler deploy
```

验证码未到时，检查 Lite Webhook 地址、Token、`simNumber` 和 D1 的 `sms_messages` 记录。
