/* 一次性 UI 验收：打开首页 → 登录 → 截三张图（hero/对话态/数据总览）
 * 跑法：node ui_shoot.js <local_key>
 * 不依赖 playwright-cli 的 daemon，直接用 playwright-core 起 Edge。
 */
const path = require("path");
const pw = require(path.join(process.env.PW_CORE, "playwright-core"));

const KEY = process.argv[2] || "";
const BASE = process.argv[3] || "http://127.0.0.1:8000";

(async () => {
  const browser = await pw.chromium.launch({
    channel: "msedge", headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.screenshot({ path: "shot_login.png" });

  // 登录
  await page.fill("#key", KEY);
  await page.click("#login");
  await page.waitForSelector("#app", { state: "visible", timeout: 15000 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: "shot_home.png" });

  // 数据总览（真实用量）
  await page.click('.nav[data-view="stats"]');
  await page.waitForTimeout(600);
  await page.screenshot({ path: "shot_stats.png" });

  // 知识库·技能
  await page.click('.nav[data-view="kb"]');
  await page.waitForTimeout(400);
  await page.screenshot({ path: "shot_kb.png" });

  // 回到对话，发一条消息（direct 档，走真实模型）
  await page.click("#navNew");
  await page.selectOption("#tierSel", "direct");
  await page.fill("#input", "用一句话解释什么是 RAG");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(15000);
  await page.screenshot({ path: "shot_chat.png", fullPage: false });

  // 控制台错误收集
  console.log("done");
  await browser.close();
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
