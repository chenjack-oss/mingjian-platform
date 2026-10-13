/* 中台化前端视觉验收：登录 → 切四个新视图（审计/智能体/数据总览成本/个人空间）→ 截多图 + 收集控制台错误
 * 跑法：PW_CORE=<node_modules> node ui_verify.js <key> <base>
 */
const path = require("path");
const pw = require(path.join(process.env.PW_CORE, "playwright-core"));

const KEY = process.argv[2] || "";
const BASE = process.argv[3] || "http://127.0.0.1:8765";
const OUT = process.argv[4] || "C:/Users/19838/Desktop/项目文件/_mj_repo/ui_shots";

const fs = require("fs");
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await pw.chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  const consoleErrors = [];
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push("PAGEERROR: " + e.message));

  await page.goto(BASE + "/", { waitUntil: "networkidle" });
  await page.screenshot({ path: OUT + "/01_login.png" });

  // 登录
  await page.fill("#key", KEY);
  await page.click("#login");
  await page.waitForSelector("#app", { state: "visible", timeout: 20000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: OUT + "/02_home.png" });

  // 智能体（P-4 动态注册，真实子 Agent 清单）
  // /health 是重端点（实测 2s+），等加载态被真实内容替换再截图
  await page.click('.nav[data-view="agents"]');
  await page.waitForFunction(
    () => !document.querySelector('#subagentList')?.textContent.includes('加载中'),
    { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(500);
  await page.screenshot({ path: OUT + "/03_agents.png" });

  // 数据总览（含 token 成本面板）
  await page.click('.nav[data-view="stats"]');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: OUT + "/04_stats_cost.png" });

  // 审计日志（P-4，空态）
  await page.click('.nav[data-view="audit"]');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: OUT + "/05_audit.png" });

  // 个人空间
  await page.click('.nav[data-view="space"]');
  await page.waitForTimeout(800);
  await page.screenshot({ path: OUT + "/06_space.png" });

  // 知识库技能
  await page.click('.nav[data-view="kb"]');
  await page.waitForTimeout(800);
  await page.screenshot({ path: OUT + "/07_kb.png" });

  // 回到对话
  await page.click("#navNew");
  await page.waitForTimeout(500);
  await page.screenshot({ path: OUT + "/08_chat.png" });

  // 检查关键 DOM 元素是否存在
  const checks = {};
  checks["审计视图存在"] = await page.locator('#view-audit').count() > 0;
  checks["成本面板存在"] = await page.locator('#costPanel').count() > 0;
  checks["子Agent清单容器"] = await page.locator('#subagentList').count() > 0;
  checks["审计导航按钮"] = await page.locator('.nav[data-view="audit"]').count() > 0;

  console.log("=== DOM 检查 ===");
  for (const [k, v] of Object.entries(checks)) console.log(`${v ? "✅" : "❌"} ${k}`);
  console.log("=== 控制台错误 ===");
  if (consoleErrors.length === 0) console.log("（无）");
  else consoleErrors.forEach(e => console.log("  " + e));
  console.log("=== 截图输出目录 ===");
  console.log(OUT);
  await browser.close();
})().catch(e => { console.error("FAIL:", e.message); process.exit(1); });
