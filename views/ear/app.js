const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const listEl = $("list");
const emptyEl = $("empty");
const keyHint = $("keyHint");
const saveMsg = $("saveMsg");
const modelSel = $("model");
const modelCustom = $("model_custom");

const PRESETS = ["qwen3-omni-flash", "qwen3.8-omni-flash", "qwen3.5-omni-flash"];

const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function applyModel(value) {
  if (PRESETS.includes(value)) {
    modelSel.value = value;
    modelCustom.hidden = true;
    modelCustom.value = "";
  } else {
    modelSel.value = "__custom__";
    modelCustom.hidden = false;
    modelCustom.value = value || "";
  }
}

modelSel.addEventListener("change", () => {
  modelCustom.hidden = modelSel.value !== "__custom__";
  if (!modelCustom.hidden) modelCustom.focus();
});

async function loadSettings() {
  try {
    const s = await bridge.apiGet("settings");
    $("enabled").checked = !!s.enabled;
    $("base_url").value = s.base_url || "";
    applyModel(s.model || "qwen3-omni-flash");
    $("only_owner_private").checked = !!s.only_owner_private;
    $("owner_qq").value = s.owner_qq || "";
    $("douyin_enabled").checked = !!s.douyin_enabled;
    $("douyin_cdp_url").value = s.douyin_cdp_url || "";
    $("daily_limit").value = s.daily_limit ?? 300;
    keyHint.textContent = s.has_key ? `已配置（尾号 ${s.key_tail || "****"}），留空不改` : "还没配置，必需填一个";
    return s;
  } catch (e) {
    saveMsg.textContent = "配置读取失败：" + e;
    return null;
  }
}

async function loadHistory() {
  try {
    const r = await bridge.apiGet("history");
    const items = r.items || [];
    listEl.innerHTML = "";
    emptyEl.hidden = items.length > 0;
    for (const it of items) {
      const li = document.createElement("li");
      li.className = "item";
      li.innerHTML =
        `<div class="meta"><span>${esc(it.time)}</span>` +
        (it.seconds ? `<span>${esc(it.seconds)}s</span>` : "") +
        `<span>来自 ${esc(it.sender || "-")}</span></div>` +
        `<div class="heard">${esc(it.heard)}</div>`;
      listEl.appendChild(li);
    }
  } catch (e) {
    statusEl.textContent = "记录读取失败：" + e;
  }
}

async function refresh() {
  const s = await loadSettings();
  if (s) {
    let used = 0;
    try {
      const st = await bridge.apiGet("status");
      used = st.used_today ?? 0;
    } catch (e) {}
    const bits = [
      s.enabled ? "已启用" : "已关闭",
      `共听 ${s.count ?? 0} 条`,
      s.daily_limit > 0 ? `今日 ${used}/${s.daily_limit}` : "不限次数",
      s.has_key ? "Key 就绪" : "⚠️ 缺 Key",
    ];
    if (s.douyin_enabled) bits.push(s.douyin_cdp_url ? "抖音已开" : "抖音开了但没填地址");
    statusEl.textContent = bits.join(" · ");
  }
  await loadHistory();
}

$("save").addEventListener("click", async () => {
  saveMsg.textContent = "保存中…";
  const chosen = modelSel.value === "__custom__" ? modelCustom.value.trim() : modelSel.value;
  if (!chosen) {
    saveMsg.textContent = "模型不能为空";
    return;
  }
  const payload = {
    enabled: $("enabled").checked,
    base_url: $("base_url").value.trim(),
    model: chosen,
    only_owner_private: $("only_owner_private").checked,
    owner_qq: $("owner_qq").value.trim(),
    douyin_enabled: $("douyin_enabled").checked,
    douyin_cdp_url: $("douyin_cdp_url").value.trim(),
    daily_limit: parseInt($("daily_limit").value || "300", 10),
  };
  const key = $("api_key").value.trim();
  if (key) payload.api_key = key;

  try {
    await bridge.apiPost("settings/save", payload);
    $("api_key").value = "";
    saveMsg.textContent = "已保存 ✓";
    await refresh();
  } catch (e) {
    saveMsg.textContent = "保存失败：" + e;
  }
});

$("refresh").addEventListener("click", refresh);

$("clear").addEventListener("click", async () => {
  if (!confirm("确定清空全部听取记录？")) return;
  try {
    await bridge.apiPost("clear", {});
    await refresh();
  } catch (e) {
    alert("清空失败：" + e);
  }
});

if (bridge && bridge.ready) {
  try { await bridge.ready(); } catch (e) {}
}
refresh();
