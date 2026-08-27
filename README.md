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

静态页面位于 `public/`，部署命令使用 `npx wrangler deploy`。如果使用 Cloudflare Pages 的纯静态部署流程，请选择 `cf-worker/public` 作为输出目录，并使用 Workers 部署而不是单独的 Pages 静态上传，以保留 API、D1 和 Durable Object。
