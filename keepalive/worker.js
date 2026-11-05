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
 * 部署前需要在 Worker 的 Variables 里配置(见 SETUP.md):
 *   REPO     文本变量  "dink1257/investment-research"
 *   GH_TOKEN 加密密钥  fine-grained PAT,仅此仓库,权限 Actions: Read and write
 */

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

    // 1) 查最近一次运行(无论 schedule 还是 dispatch 触发的都算)
    const listRes = await fetch(`${base}/runs?per_page=3`, { headers });
    if (!listRes.ok) {
      throw new Error(`list runs failed: ${listRes.status} ${await listRes.text()}`);
    }
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
    const dispRes = await fetch(`${base}/dispatches`, {
      method: "POST",
      headers,
      body: JSON.stringify({ ref: "main" }),
    });
    if (dispRes.status !== 204) {
      throw new Error(`dispatch failed: ${dispRes.status} ${await dispRes.text()}`);
    }
    console.log(
      `[${event.cron}] dispatched workflow (latest run was ${newest ? `#${newest.run_number}, ${newestAgeMin.toFixed(1)} min ago` : "none"})`
    );
  },
};
