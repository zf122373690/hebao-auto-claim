export interface Env {
  DB: D1Database;
  CLAIM_RUNNER: DurableObjectNamespace;
  ASSETS: Fetcher;
  BASE_URL: string;
  ACCESS_TOKEN: string;
  AES_KEY: string;
  WEBHOOK_SECRET?: string;
}

const PRODUCTS: Record<string, { rightsCode: string; legalRightsId: string; offerId: string; name: string }> = {
  "1": {rightsCode:"QY1701763685438", legalRightsId:"1731948543350599682", offerId:"", name:"和包出行体验会员（1元）"},
  "2": {rightsCode:"QY1701761448805", legalRightsId:"1731939162227888130", offerId:"", name:"和包出行体验会员（2元）"},
  "5": {rightsCode:"QY1701758647806", legalRightsId:"1731927413990588418", offerId:"19197", name:"和包出行体验会员（5元）"},
  "30": {rightsCode:"QY1737687753540", legalRightsId:"1882625005885779970", offerId:"40625", name:"美宜佳满减券（30元）"}
};
const json = (x: unknown, status=200) => new Response(JSON.stringify(x), {status, headers:{"content-type":"application/json;charset=utf-8"}});
const body = async (r: Request) => await r.json().catch(() => ({})) as Record<string, unknown>;
const codeFrom = (s: string, digits?: number) => { if (!/(验证码|动态码|校验码|随机码|口令|verification\s*code)/i.test(s)) return null; const pattern="\\b\\d{" + (digits ?? 4) + "," + (digits ?? 8) + "}\\b"; return s.match(new RegExp(pattern))?.[0] ?? null; };
const phoneFrom = (s: string) => s.match(/1[3-9]\d{9}/)?.[0] ?? null;

export default { async fetch(req: Request, env: Env): Promise<Response> {
  const u = new URL(req.url), path = u.pathname.replace(/\/$/, "");
  if (path === "/healthz") return json({ok:true});
  if (path === "/api/products") return json(Object.entries(PRODUCTS).map(([amount,p]) => ({amount,...p})));
  if (path === "/api/accounts" && req.method === "GET") { const result=await env.DB.prepare("SELECT phone,label,level,last_login,(sess_key != '') logged_in FROM accounts ORDER BY created_at").all(); return json({accounts:result.results}); }
  if (path === "/api/accounts" && req.method === "POST") { const b=await body(req), phone=String(b.phone??""); if(!/^\d{11}$/.test(phone)) return json({ok:false,msg:"手机号格式不正确"},400); const action=String(b.action??"add"); if(action==="delete") await env.DB.prepare("DELETE FROM accounts WHERE phone=?").bind(phone).run(); else await env.DB.prepare("INSERT INTO accounts(phone,label,created_at) VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET label=excluded.label").bind(phone,String(b.label??""),Date.now()).run(); return json({ok:true}); }
  if ((path === "/api/hooks" || path === "/webhook/sms") && req.method === "POST") { if(env.WEBHOOK_SECRET && req.headers.get("x-webhook-secret") !== env.WEBHOOK_SECRET) return json({ok:false},401); const raw=await req.text(); let parsed: unknown=raw; try { parsed=raw ? JSON.parse(raw) : raw; } catch {} const payload=typeof parsed === "object" && parsed ? parsed as Record<string,unknown> : {}; const text=String(payload.text ?? payload.content ?? payload.body ?? payload.message ?? parsed); const explicitPhone=String(payload.phone ?? ""); const phone=/^1[3-9]\d{9}$/.test(explicitPhone) ? explicitPhone : phoneFrom(text); const code=codeFrom(text); const now=Date.now(); await env.DB.prepare("INSERT INTO sms_messages(body,code,phone,digits,received_at,expires_at) VALUES(?,?,?,?,?,?)").bind(text,code,phone,code?.length ?? null,now,now+10*60*1000).run(); return json({ok:true,code,phone}); }
  if (path === "/api/codes" && req.method === "GET") { const phone=u.searchParams.get("phone"); const digits=Number(u.searchParams.get("digits")||0); const row=await env.DB.prepare("SELECT id,code,phone,received_at FROM sms_messages WHERE consumed_at IS NULL AND expires_at>? AND code IS NOT NULL AND (?=0 OR digits=?) AND (? IS NULL OR phone=? ) ORDER BY received_at ASC LIMIT 1").bind(Date.now(),digits,digits,phone,phone).first(); return json(row ?? null); }
  if (path === "/api/codes/consume" && req.method === "POST") { const b=await body(req), id=Number(b.id); if(!Number.isInteger(id)||id<=0) return json({ok:false,msg:"验证码记录无效"},400); const result=await env.DB.prepare("UPDATE sms_messages SET consumed_at=? WHERE id=? AND consumed_at IS NULL AND expires_at>?").bind(Date.now(),id,Date.now()).run(); return json({ok:Boolean(result.meta.changes)}); }
  if (path === "/api/start" && req.method === "POST") { const b=await body(req), phone=String(b.phone??""); if(!/^\d{11}$/.test(phone)) return json({ok:false,msg:"手机号格式不正确"},400); const id=env.CLAIM_RUNNER.idFromName(phone), stub=env.CLAIM_RUNNER.get(id); return stub.fetch("https://runner/start",{method:"POST",body:JSON.stringify({phone,...b})}); }
  if (path === "/api/stop" && req.method === "POST") { const b=await body(req), stub=env.CLAIM_RUNNER.get(env.CLAIM_RUNNER.idFromName(String(b.phone??""))); return stub.fetch("https://runner/stop",{method:"POST"}); }
  if (!path.startsWith("/api/") && path !== "/webhook/sms") return env.ASSETS.fetch(req);
  return json({ok:false,msg:"not found"},404);
}, async scheduled(_event: ScheduledEvent, env: Env) { const rows=await env.DB.prepare("SELECT value FROM settings WHERE key='schedule'").first<{value:string}>(); if(!rows) return; const cfg=JSON.parse(rows.value); if(!cfg.enabled) return; for(const phone of cfg.phones??[]) { const stub=env.CLAIM_RUNNER.get(env.CLAIM_RUNNER.idFromName(phone)); await stub.fetch("https://runner/start",{method:"POST",body:JSON.stringify({phone,skipLogin:true,...cfg})}); } } };

export class ClaimRunner {
  constructor(private state: DurableObjectState, private env: Env) {}
  async fetch(req: Request) { const path=new URL(req.url).pathname; if(path==="/stop"){await this.state.storage.put("run",{state:"stopped"});return json({ok:true});} if(path!=="/start") return json({ok:false},404); const b=await req.json(); const old=await this.state.storage.get<any>("run"); if(old?.state==="running") return json({ok:false,msg:"已有任务在运行"},409); await this.state.storage.put("run",{state:"waiting_login",payload:b,updatedAt:Date.now()}); return json({ok:true,msg:"任务已排队，验证码到达后继续"}); }
}
