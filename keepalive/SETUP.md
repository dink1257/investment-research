# investment-research 看门狗触发器 · 部署指南

> 解决的问题:GitHub Actions 的 schedule 定时器在新仓库上跑得很稀疏
> (2026-10-30 部署,到 11-05 一共 26 次运行,15 分钟一档应有的约 570 次),
> 异动时段经常无人盯,影子复核要的运行历史也攒得慢。
>
> 方案:用 Cloudflare Worker(免费)做"看门狗"。它每 10 分钟醒来,
> 先查 GitHub 最近一次运行是否新鲜;只有 GitHub 自带调度掉线超过 25 分钟时
> 才补发一次,平时什么都不做——不会重复运行、不会多发飞书、不会多花钱。
> 改编自旧系统 stock-alerts 的 keepalive/(同一套逻辑,已备好未部署),
> 只改了仓库、workflow 文件名和检查节律。

## 免费层够用吗(2026-11-05 核过 Cloudflare 文档)

- 每天 10 万次请求,本 Worker 每天约 144 次(每 10 分钟一次),占约 0.15%。
- 免费账号最多 5 个 cron 触发器,本 Worker 只用 1 个。
- 每次调用 CPU 上限 10 毫秒,但等网络返回的时间不算 CPU,两次 GitHub API
  调用都在限额内。
- 免费不用绑卡,超了也不会悄悄扣费(到量就停)。
- KV 免费层:每天 10 万次读、1000 次写。本 Worker 每天约 144 次读、
  约 300 次写(每轮写 1–2 次);大阪 VPS 每 5 分钟探测一次约 288 次读。
  合计远低于限额。
- 结论:免费层够用,不用升级。

## 前提

- Cloudflare 免费账号(注册即可,不用绑卡)
- 一个 GitHub fine-grained token,只给 `dink1257/investment-research` 这一个
  仓库的 **Actions: Read and write** 权限;建议有效期 90 天,到期前日历提醒
  更换;它只存放在 Cloudflare 的加密变量里,不经过聊天。
- 注意:muse-push-deploy 那个 token 是 Contents 读写,不能用来触发
  workflow,所以看门狗要单独建一把 Actions 权限的 token。
- 一个 KV 命名空间(免费层,存健康状态):Cloudflare Dashboard → Storage
  → KV → Create a namespace,名字如 `investment-research-watchdog-state`。
  记下它的 ID,部署时绑定到 Worker(见步骤 4)。

## 步骤(约 15 分钟)

1. **建 token**:GitHub → Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token
   - Repository access: Only select repositories → `dink1257/investment-research`
   - Repository permissions → Actions → **Read and write**(只需要这一项)
   - Expiration:建议 90 天
   - 复制 token(只显示一次)

2. **建 KV 命名空间**:Cloudflare Dashboard → Storage → KV →
   Create a namespace,名字如 `investment-research-watchdog-state`。
   (若已建过,跳过这步。)

3. **建 Worker**:Cloudflare Dashboard → Workers & Pages → Create Worker →
   把 `worker.js` 的内容粘贴进去 → Deploy

4. **配变量 + 绑 KV**:Worker → Settings → Variables
   - 文本变量 `REPO` = `dink1257/investment-research`
   - 加密变量(点 Encrypt/Add secret)`GH_TOKEN` = 第 1 步的 token
   - KV Namespace Bindings → Add binding:变量名 `WATCHDOG_STATE`,
     选第 2 步建的命名空间 → Deploy(改绑定后要点 Deploy 才生效)

5. **加定时**:Worker → Settings → Triggers → Cron Triggers,添加一条
   `*/10 * * * *`(Cloudflare cron 是 UTC,和 GitHub 的 cron 含义一致)

6. **验证**:等下一个 10 分钟整点,看三处
   - Cloudflare → Worker → Logs:应看到 `github schedule healthy, skip`
     (说明 GitHub 自带调度正常,看门狗待命)或 `dispatched workflow`
     (说明补发了一次)
   - GitHub → investment-research → Actions 页面:应出现新的运行记录
   - 健康接口:浏览器或 curl 打开
     `https://investment-research-keepalive.dink1257.workers.dev/healthz`,
     应返回 JSON,`last_tick_at` 是最近一个整点 10 分钟内的 UTC 时间,
     `last_check_status` 是 `ok`。若返回 `unknown` 且时间全是 null,
     说明定时任务还没跑过第一轮,等下一个整点再看。

## 健康接口 /healthz(2026-11-08 新增,供大阪 VPS 探测)

- 地址:`https://investment-research-keepalive.dink1257.workers.dev/healthz`
  (GET,无认证,见下)。
- 返回示例(真实值随运行变化):
  `{"schema_version":1,"last_tick_at":"2026-11-08T15:30:00.000Z","last_github_check_success_at":"2026-11-08T15:30:01.000Z","last_check_status":"ok","last_error_code":null}`
- 字段:
  - `last_tick_at`:最近一次定时检查启动时间(UTC)。每次 cron 醒来就更新,
    不管 GitHub 查没查成——这是“Worker 跑了”的证据。
  - `last_github_check_success_at`:最近一次成功查询 GitHub 的时间(UTC)。
    认证失败、网络异常时不更新——这是“GitHub 查成了”的证据,两者分开。
  - `last_check_status`:`ok` / `error` / `unknown`(从未跑过)。
  - `last_error_code`:无错误时为 null;否则为 `github_list_http_<状态码>`、
    `github_list_network`、`dispatch_http_<状态码>`、`dispatch_network`
    之一(只含状态码,不含响应正文)。
- 保证:
  - 访问接口本身不刷新任何时间,只读 KV 里定时任务保存的状态。
  - 返回 `Cache-Control: no-store`,不被缓存。
  - 不返回 token、日志正文、私有仓库内容。无认证:返回的只是公开仓库
    公开运行记录的时间戳与状态,无敏感信息;若以后需要认证,再加独立的
    只读凭据(不复用 GH_TOKEN)。
  - KV 是最终一致,写入后通常数秒内各节点可见;VPS 每 5 分钟探测一次,
    这个延迟可忽略。KV 若暂时不可用,看门狗仍正常补发,只是健康状态
    不更新(宁可误报,不可静默)。
- VPS 侧约定(由 Codex 负责):每 5 分钟探测一次;连续失败先重试确认;
  `last_tick_at` 超过 30 分钟未更新告警,恢复后通知。阈值联调后调整。

## 注意事项

- workflow 文件里的 `workflow_dispatch:` 必须保留(已确认,2026-10-30 用户
  手动触发成功过),否则补发接口会 404。
- 部署看门狗之后,**GitHub 自带的 schedule 段建议保留**(双保险)。
  看门狗只在掉线时补发,两者不会在同一时段各跑一次。
- 看门狗只保证"有人去触发",GitHub 那边排队延迟它管不了,所以不能保证
  15 分钟一档,只能保证不断档太久。
- 如果 GitHub 调度彻底停了,看门狗每天最多补发约 144 次,和 workflow
  自己的 96 次/天一个量级,不会失控。
- token 到期前记得更换:Cloudflare → Worker → Settings → Variables →
  更新 `GH_TOKEN` 即可,不用改代码。
- KV 命名空间创建时数据位置选「标准 (Standard)」;「管辖权 (Jurisdiction)」
  会限制区域且建后不可改,不要选。
- Dashboard 代码编辑器的粘贴可能因剪贴板权限被拒(Ctrl+V 也无效):
  可用编辑器内查找替换(正则跨行匹配整文件)把新代码写进去,替换后
  核对行数、确认 0 Problems 再点 Deploy。
- Cloudflare 登录有 Turnstile + 验证器两步验证;Turnstile 若反复失败可
  换个浏览器环境重试;验证码只能由账号本人在浏览器里输入,不要发到聊天里。
