# 和包 Cloudflare 迁移版

## 初始化

```powershell
npm install
npx wrangler d1 create hebao
npx wrangler d1 execute hebao --remote --file=schema.sql
npx wrangler secret put ACCESS_TOKEN
npx wrangler secret put AES_KEY
npx wrangler secret put WEBHOOK_SECRET
npx wrangler deploy
```

把 `wrangler.toml` 中的 `database_id` 替换为创建出的 ID。短信转发器 POST 到 `/api/hooks`，并配置 `x-webhook-secret`。验证码写入 D1 的 `sms_messages` 表，默认 10 分钟过期；领取流程通过 `/api/codes` 获取，再调用 `/api/codes/consume` 原子消费。

当前目录是迁移骨架：账号、短信、任务状态、Cron 和 Cloudflare 绑定已完成。下一步需要把原 Python 的 AES-CBC/签名/API 调用逐个移植到 `src/core.ts`，再把 Durable Object 状态机接上验证码消费和领取间隔。

静态页面位于 `public/`，部署命令使用 `npx wrangler deploy`。如果使用 Cloudflare Pages 的纯静态部署流程，请选择 `public` 作为输出目录，并使用 Workers 部署而不是单独的 Pages 静态上传，以保留 API、D1 和 Durable Object。

## T3 Lite 短信 Webhook

在 Lite 固件的推送通道中选择“自定义 Webhook”，填写：

- Webhook URL：`https://你的 Worker 域名/api/hooks`
- 鉴权 Token：与 Worker Secret `WEBHOOK_SECRET` 相同；未配置 Secret 时留空
- 自定义模板：

```text
JSON:{"type":"sms","sender":"{{短信号码}}","message":"{{短信内容}}","sim":"{{SIM标签}}","simNumber":"{{卡号}}","timestamp":"{{时间}}"}
```

Worker 同时接受 Lite 固件发送的 `Authorization: Bearer <Token>` 和 `X-Device-Token`。收到短信后从 `message` 提取验证码，以 `simNumber` 作为接收手机号，写入 D1 的 `sms_messages` 表；验证码默认 10 分钟过期，领取流程读取后会通过 `/api/codes/consume` 标记为已消费。
