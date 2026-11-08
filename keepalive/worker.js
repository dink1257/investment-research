/**
 * investment-research 看门狗触发器 (Cloudflare Worker, 免费版即可)
 *
 * 背景:
 * GitHub Actions 的 schedule 定时器在新仓库上跑得很稀疏(部署 6 天只跑了
 * 25 次定时运行,15 分钟一档应有的约 570 次),导致异动无人盯。
 * 本 Worker 用 Cloudflare 的 cron 每 10 分钟醒来,检查 GitHub 上最近一次
 * 运行是否新鲜;只有当 GitHub 自带调度掉线超过 25 分钟时才补发一次
 * workflow_dispatch。平时 GitHub 正常时,本 Worker 什么都不做——不会重复跑、
 * 不会多发飞书、不会多花钱。
 *
 * 健康接口 (供大阪 VPS 探测,2026-11-08 新增):
 *   GET https://investment-research-keepalive.dink1257.workers.dev/healthz
 * 返回定时任务保存在 KV 里的最新状态(JSON),只读,访问不刷新任何时间:
 *   {
 *     "schema_version": 1,
 *     "last_tick_at": "最近一次定时检查启动时间,UTC ISO",
 *     "last_github_check_success_at": "最近一次成功查询 GitHub 的时间,UTC ISO",
 *     "last_check_status": "ok" | "error" | "unknown",
 *     "last_error_code": null | "github_list_http_<状态码>"
 *                         | "github_list_network" | "dispatch_http_<状态码>"
 *                         | "dispatch_network"
 *   }
 * 无认证:返回的只是公开仓库公开运行记录的时间戳与状态,无 token、
 * 无日志正文、无私有内容;若以后需要认证,再加独立的只读凭据
 * (不复用 GH_TOKEN)。其他路径一律 404。
 *
 * 状态存储:Cloudflare KV(免费层),绑定名 WATCHDOG_STATE,键 "health"。
 * 定时任务每轮写 KV(启动时写 last_tick_at,GitHub 查成功后写成功时间
 * 与 ok,失败时只写 error 状态、不更新成功时间)。KV 是最终一致,
 * 写入后通常数秒内各节点可见;VPS 每 5 分钟探测一次,这个延迟可忽略。
 * KV 若暂时不可用,看门狗仍正常补发,只是健康状态不更新(宁可误报,
 * 不可静默)。
 *
 * 部署前需要在 Worker 的 Variables 里配置(见 SETUP.md):
 *   REPO            文本变量  "dink1257/investment-research"
 *   GH_TOKEN        加密密钥  fine-grained PAT,仅此仓库,权限 Actions: Read and write
 *   WATCHDOG_STATE  KV 绑定   KV 命名空间,存健康状态(见 SETUP.md)
 */

const HEALTH_KEY = "health";

function defaultState() {
  return {
    schema_version: 1,
    last_tick_at: null,
    last_github_check_success_at: null,
    last_check_status: "unknown",
    last_error_code: null,
  };
}

// 读状态:KV 不可用时返回默认值并记警告,绝不能让状态存储拖垮补发主流程。
async function readState(env) {
  try {
    const stored = await env.WATCHDOG_STATE.get(HEALTH_KEY, "json");
    return { ...defaultState(), ...(stored || {}) };
  } catch (e) {
    console.warn("health state read failed, using defaults:", e);
    return defaultState();
  }
}

// 写状态:KV 不可用时只记警告,不抛错。
async function writeState(env, state) {
  try {
    await env.WATCHDOG_STATE.put(HEALTH_KEY, JSON.stringify(state));
  } catch (e) {
    console.warn("health state write failed:", e);
  }
}

export default {
  async scheduled(event, env, ctx) {
    const base =
      `https://api.github.com/repos/${env.REPO}` +
      `/actions/workflows/monitor.yml`;
    const headers = {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "investment-research-keepalive/1.0",
    };

    // 每次定时醒来先记一笔:定时任务确实运行了(UTC)。
    // 注意:这是“Worker 跑了”的时间,不是“GitHub 查成功了”的时间,
    // 两者分开记,见下。
    const tickAt = new Date().toISOString();
    const state = await readState(env);
    state.last_tick_at = tickAt;
    await writeState(env, state);

    // 1) 查最近一次运行(无论 schedule 还是 dispatch 触发的都算)
    let listRes;
    try {
      listRes = await fetch(`${base}/runs?per_page=3`, { headers });
    } catch (e) {
      state.last_check_status = "error";
      state.last_error_code = "github_list_network";
      await writeState(env, state);
      throw e;
    }
    if (!listRes.ok) {
      state.last_check_status = "error";
      state.last_error_code = `github_list_http_${listRes.status}`;
      await writeState(env, state);
      throw new Error(`list runs failed: ${listRes.status} ${await listRes.text()}`);
    }
    // GitHub 查询成功:只在这里更新成功时间。认证失败等异常走上面
    // 的分支,不会更新这个时间。
    state.last_github_check_success_at = new Date().toISOString();
    state.last_check_status = "ok";
    state.last_error_code = null;
    await writeState(env, state);

    const runs = (await listRes.json()).workflow_runs || [];
    const now = Date.now();
    const newest = runs[0];
    const newestAgeMin = newest
      ? (now - new Date(newest.run_started_at || newest.created_at).getTime()) / 60000
      : Infinity;

    // 2) 25 分钟内有过运行(进行中/排队中/刚完成) → GitHub 调度正常,什么都不做
    if (
      newest &&
      newestAgeMin < 25 &&
      (newest.status === "in_progress" ||
        newest.status === "queued" ||
        newest.status === "completed")
    ) {
      console.log(
        `[${event.cron}] github schedule healthy, skip (latest run #${newest.run_number}, ${newestAgeMin.toFixed(1)} min ago)`
      );
      return;
    }

    // 3) 否则补发一次 dispatch
    let dispRes;
    try {
      dispRes = await fetch(`${base}/dispatches`, {
        method: "POST",
        headers,
        body: JSON.stringify({ ref: "main" }),
      });
    } catch (e) {
      state.last_check_status = "error";
      state.last_error_code = "dispatch_network";
      await writeState(env, state);
      throw e;
    }
    if (dispRes.status !== 204) {
      state.last_check_status = "error";
      state.last_error_code = `dispatch_http_${dispRes.status}`;
      await writeState(env, state);
      throw new Error(`dispatch failed: ${dispRes.status} ${await dispRes.text()}`);
    }
    console.log(
      `[${event.cron}] dispatched workflow (latest run was ${newest ? `#${newest.run_number}, ${newestAgeMin.toFixed(1)} min ago` : "none"})`
    );
  },

  // 健康接口:只读。访问不刷新任何时间,只返回 KV 里定时任务保存的状态。
  // 其他路径一律 404(本 Worker 不提供别的页面)。
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname !== "/healthz" || request.method !== "GET") {
      return new Response("Not found", { status: 404 });
    }
    const state = await readState(env);
    return new Response(JSON.stringify(state), {
      headers: {
        "content-type": "application/json",
        "cache-control": "no-store",
      },
    });
  },
};
