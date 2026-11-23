const form = document.querySelector("#generation");
const submitStatus = document.querySelector("#submit-status");
const historyStatus = document.querySelector("#history-status");
const list = document.querySelector("#jobs");
const more = document.querySelector("#more");
const refresh = document.querySelector("#refresh");
const trackSearch = document.querySelector("#track-search");
const trackStatus = document.querySelector("#track-status");
const trackResults = document.querySelector("#track-results");
const creativeChat = document.querySelector("#creative-chat");
const chatSubmit = creativeChat.querySelector("button");
const creativeMessages = document.querySelector("#creative-messages");
const creativeStatus = document.querySelector("#creative-status");
const useBrief = document.querySelector("#use-brief");
const previewBrief = document.querySelector("#preview-brief");
const previewHint = document.querySelector("#preview-hint");
const deepThink = document.querySelector("input[name=deep-think]");
const webSearch = document.querySelector("input[name=web-search]");
const creativeModel = document.querySelector("#creative-model");
const conversationList = document.querySelector("#conversation-list");
const newConversation = document.querySelector("#new-conversation");
const deleteConversation = document.querySelector("#delete-conversation");
const assetForm = document.querySelector("#asset-upload");
const assetList = document.querySelector("#asset-list");
const assetStatus = document.querySelector("#asset-status");
const assetSearch = document.querySelector("#asset-search");
const assetSearchStatus = document.querySelector("#asset-search-status");
const assetSearchResults = document.querySelector("#asset-search-results");
const referenceAssetSelect = form.elements.reference_audio_asset_id;
const mixAssetDialog = document.querySelector("#mix-asset-dialog");
const mixAssetOptions = document.querySelector("#mix-asset-options");
const mixAssetQuery = document.querySelector("#mix-asset-query");
const mixAssetType = document.querySelector("#mix-asset-type");
const mixPickerStatus = document.querySelector("#mix-picker-status");
const mixSelectionSummary = document.querySelector("#mix-selection-summary");
const clearMixAssets = document.querySelector("#clear-mix-assets");
let audioAssets = [];
let selectedMixAssetIds = new Set();
let draftMixAssetIds = new Set();
const trainingStatus = document.querySelector("#training-status");
const prepareTraining = document.querySelector("#prepare-training");
const preprocessTraining = document.querySelector("#preprocess-training");
const startTraining = document.querySelector("#start-training");
const stopTraining = document.querySelector("#stop-training");
const loadTrainingAdapter = document.querySelector("#load-training-adapter");
let trainingDatasetId = null;
let trainingRunId = null;
const projectCreate = document.querySelector("#project-create");
const projectStatus = document.querySelector("#project-status");
const projectsElement = document.querySelector("#projects");
const refreshProjects = document.querySelector("#refresh-projects");
let projects = [];
let activeProjectId = null;
document.querySelector("#logout").addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  location.assign("/login");
});
const jobs = new Map();
let cursor = null;
let latestBrief = null;
let chatHistory = [];
let activeConversation = null;
const assistantGreeting = "告诉我想写一首什么样的歌。我会和你一起打磨歌词；你确认后，再把歌词送到 ACE-Step 生成歌曲。";

async function loadAssets() {
  const assets = await api("/audio-assets");
  audioAssets = assets;
  assetList.replaceChildren();
  referenceAssetSelect.replaceChildren(new Option("不使用参考音频", ""));
  selectedMixAssetIds = new Set([...selectedMixAssetIds].filter(id => assets.some(asset => asset.id === id && isMixEligible(asset))));
  for (const asset of assets) {
    const row = document.createElement("li");
    const label = document.createElement("span");
    const typeName = { original: "原始素材", stem: "Stem 分轨", sample: "Sample 采样" }[asset.type] || asset.type;
    label.textContent = `${typeName} · ${asset.filename} · ${(asset.tags || []).join(" / ")} · ${asset.caption || "暂无声音描述"} · 授权：${asset.license_status}`;
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = asset.audio_url;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      try { await api(`/audio-assets/${asset.id}`, { method: "DELETE" }); await loadAssets(); }
      catch (error) { assetStatus.textContent = error.message; }
    });
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary";
    edit.textContent = "编辑描述/授权";
    edit.addEventListener("click", async () => {
      const caption = window.prompt("声音描述（用于文字检索和训练集）", asset.caption || "");
      if (caption === null) return;
      const license = window.prompt("使用授权：unknown / owned / licensed / public_domain", asset.license_status || "unknown");
      if (license === null) return;
      try {
        await api(`/audio-assets/${asset.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ caption, license_status: license }) });
        await loadAssets();
      } catch (error) { assetStatus.textContent = error.message; }
    });
    const trainingPick = document.createElement("input");
    trainingPick.type = "checkbox";
    trainingPick.name = "training_asset_id";
    trainingPick.value = asset.id;
    trainingPick.disabled = asset.type !== "original" || asset.license_status === "unknown" || !asset.caption;
    trainingPick.setAttribute("aria-label", `将 ${asset.filename} 加入 LoKr 训练集`);
    const referenceOption = new Option(`${asset.filename} · ${asset.caption || (asset.tags || []).join(" / ")}`, asset.id);
    referenceAssetSelect.add(referenceOption);
    referenceOption.disabled = asset.license_status === "unknown";
    row.append(trainingPick, label, audio, edit, remove);
    assetList.append(row);
  }
  updateMixSelectionSummary();
}

function isMixEligible(asset) {
  const audioFile = [".wav", ".mp3", ".flac", ".ogg", ".aiff", ".aif"].some(ext => asset.filename.toLowerCase().endsWith(ext));
  return audioFile && asset.license_status !== "unknown";
}

function updateMixSelectionSummary() {
  const selected = audioAssets.filter(asset => selectedMixAssetIds.has(asset.id));
  mixSelectionSummary.textContent = selected.length
    ? `已选择 ${selected.length} 条：${selected.map(asset => asset.filename).join("、")}`
    : "未选择素材";
  document.querySelector("#open-mix-picker").textContent = selected.length ? `修改选择（${selected.length}）` : "从素材库选择";
  clearMixAssets.hidden = selected.length === 0;
}

function renderMixAssetPicker() {
  const query = mixAssetQuery.value.trim().toLocaleLowerCase();
  const type = mixAssetType.value;
  const filtered = audioAssets.filter(asset => {
    const searchable = [asset.filename, asset.caption, asset.mood, asset.use_case, ...(asset.tags || [])].join(" ").toLocaleLowerCase();
    return isMixEligible(asset) && (!type || asset.type === type) && (!query || searchable.includes(query));
  });
  mixAssetOptions.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement("li");
    empty.textContent = audioAssets.length ? "没有符合条件且已确认授权的音频素材。" : "素材库还没有可用素材。";
    mixAssetOptions.append(empty);
  }
  for (const asset of filtered) {
    const row = document.createElement("li");
    row.className = "mix-asset-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = asset.id;
    checkbox.checked = draftMixAssetIds.has(asset.id);
    checkbox.setAttribute("aria-label", `选择 ${asset.filename}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked && draftMixAssetIds.size >= 4) {
        checkbox.checked = false;
        mixPickerStatus.textContent = "最多选择 4 条素材。";
        return;
      }
      if (checkbox.checked) draftMixAssetIds.add(asset.id);
      else draftMixAssetIds.delete(asset.id);
      mixPickerStatus.textContent = `已勾选 ${draftMixAssetIds.size}/4 条素材。`;
    });
    const details = document.createElement("span");
    const typeName = { original: "原始素材", stem: "Stem 分轨", sample: "Sample 采样" }[asset.type] || asset.type;
    details.textContent = `${asset.filename} · ${typeName} · ${asset.caption || (asset.tags || []).join(" / ") || "暂无描述"}`;
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.src = asset.audio_url;
    row.append(checkbox, details, audio);
    mixAssetOptions.append(row);
  }
  mixPickerStatus.textContent = `显示 ${filtered.length} 条可用于混入的素材；授权状态未知的素材不会列入。`;
}

document.querySelector("#open-mix-picker").addEventListener("click", () => {
  draftMixAssetIds = new Set(selectedMixAssetIds);
  renderMixAssetPicker();
  mixAssetDialog.showModal();
});
document.querySelector("#close-mix-picker").addEventListener("click", () => mixAssetDialog.close());
document.querySelector("#cancel-mix-selection").addEventListener("click", () => mixAssetDialog.close());
document.querySelector("#apply-mix-selection").addEventListener("click", () => {
  selectedMixAssetIds = new Set(draftMixAssetIds);
  updateMixSelectionSummary();
  mixAssetDialog.close();
});
mixAssetQuery.addEventListener("input", renderMixAssetPicker);
mixAssetType.addEventListener("change", renderMixAssetPicker);
clearMixAssets.addEventListener("click", () => {
  selectedMixAssetIds.clear();
  updateMixSelectionSummary();
});

assetSearch.addEventListener("submit", async event => {
  event.preventDefault();
  assetSearchStatus.textContent = "正在检索素材…";
  assetSearchResults.replaceChildren();
  try {
    const results = await api("/audio-assets/search", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: new FormData(assetSearch).get("query"), limit: 10 }) });
    assetSearchStatus.textContent = `找到 ${results.length} 条相关素材。`;
    results.forEach(asset => {
      const item = document.createElement("li");
      const text = document.createElement("span");
      text.textContent = `${asset.filename} · ${asset.caption || (asset.tags || []).join(" / ")} · 相似度 ${Number(asset.score || 0).toFixed(2)}`;
      const listen = document.createElement("audio"); listen.controls = true; listen.src = asset.audio_url;
      const reference = document.createElement("button"); reference.type = "button"; reference.className = "secondary"; reference.textContent = "设为参考音频";
      reference.addEventListener("click", () => { referenceAssetSelect.value = asset.id; document.querySelector("#generation").scrollIntoView({ behavior: "smooth" }); });
      reference.disabled = asset.license_status === "unknown";
      item.append(text, listen, reference); assetSearchResults.append(item);
    });
  } catch (error) { assetSearchStatus.textContent = error.message; }
});

prepareTraining.addEventListener("click", async () => {
  const asset_ids = [...document.querySelectorAll('input[name="training_asset_id"]:checked')].map(input => input.value);
  if (!asset_ids.length) { trainingStatus.textContent = "请至少选择一首已标注并确认授权的完整歌曲。"; return; }
  prepareTraining.disabled = true;
  trainingStatus.textContent = "正在复制素材并让 ACE-Step 扫描训练集…";
  try {
    const dataset = await api("/audio-training/datasets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ asset_ids }) });
    trainingDatasetId = dataset.id;
    trainingRunId = null;
    loadTrainingAdapter.hidden = true;
    preprocessTraining.disabled = false;
    startTraining.disabled = true;
    trainingStatus.textContent = `训练集已载入，包含 ${dataset.sample_count} 首歌曲。${dataset.message || ""}`;
  } catch (error) { trainingStatus.textContent = error.message; }
  finally { prepareTraining.disabled = false; }
});

preprocessTraining.addEventListener("click", async () => {
  if (!trainingDatasetId) return;
  preprocessTraining.disabled = true;
  trainingStatus.textContent = "已提交预处理，正在准备训练张量…";
  try {
    await api(`/audio-training/datasets/${trainingDatasetId}/preprocess`, { method: "POST" });
    const pollPreprocess = async () => {
      const state = await api(`/audio-training/datasets/${trainingDatasetId}/preprocess`);
      trainingStatus.textContent = `预处理：${state.status} ${state.progress || ""}`;
      if (state.status === "completed") { startTraining.disabled = false; return; }
      if (state.status === "failed") { preprocessTraining.disabled = false; trainingStatus.textContent = state.error || state.progress || "预处理失败。"; return; }
      setTimeout(() => pollPreprocess().catch(error => { preprocessTraining.disabled = false; trainingStatus.textContent = error.message; }), 5000);
    };
    setTimeout(() => pollPreprocess().catch(error => { preprocessTraining.disabled = false; trainingStatus.textContent = error.message; }), 1500);
  } catch (error) { preprocessTraining.disabled = false; trainingStatus.textContent = error.message; }
});

startTraining.addEventListener("click", async () => {
  if (!trainingDatasetId) return;
  startTraining.disabled = true;
  trainingStatus.textContent = "正在向 ACE-Step 提交 LoKr 训练…";
  try {
    const run = await api("/audio-training/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: trainingDatasetId, train_epochs: Number(document.querySelector("#training-epochs").value) }) });
    trainingRunId = run.id;
    stopTraining.hidden = false;
    const pollTraining = async () => {
      const state = await api(`/audio-training/runs/${trainingRunId}`);
      trainingStatus.textContent = `训练状态：${state.status || "进行中"} · Epoch ${state.current_epoch || 0} · Loss ${state.current_loss ?? "—"}`;
      if (!state.is_training) {
        stopTraining.hidden = true;
        loadTrainingAdapter.hidden = !state.loadable;
        return;
      }
      setTimeout(() => pollTraining().catch(error => { trainingStatus.textContent = error.message; }), 10000);
    };
    pollTraining().catch(error => { trainingStatus.textContent = error.message; });
  } catch (error) { startTraining.disabled = false; trainingStatus.textContent = error.message; }
});

loadTrainingAdapter.addEventListener("click", async () => {
  if (!trainingRunId) return;
  loadTrainingAdapter.disabled = true;
  try {
    const result = await api(`/audio-training/runs/${trainingRunId}/load`, { method: "POST" });
    trainingStatus.textContent = result.message || "LoKr 适配器已加载到 ACE-Step。后续人声歌曲生成将使用当前模型状态。";
  } catch (error) { trainingStatus.textContent = error.message; }
  finally { loadTrainingAdapter.disabled = false; }
});

stopTraining.addEventListener("click", async () => {
  if (!trainingRunId) return;
  try { await api(`/audio-training/runs/${trainingRunId}/stop`, { method: "POST" }); trainingStatus.textContent = "已发送停止请求。"; }
  catch (error) { trainingStatus.textContent = error.message; }
});

assetForm.addEventListener("submit", async event => {
  event.preventDefault();
  assetStatus.textContent = "正在上传素材…";
  try {
    const response = await fetch("/audio-assets", { method: "POST", body: new FormData(assetForm) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "上传失败");
    assetForm.reset();
    assetStatus.textContent = "素材已加入曲库。";
    await loadAssets();
  } catch (error) { assetStatus.textContent = error.message; }
});
loadAssets().catch(error => { assetStatus.textContent = error.message; });

const statuses = { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "生成失败" };
const instrumentLabels = { piano: "钢琴", guzheng: "古筝", music_box: "八音盒", guitar: "木吉他", synth_pad: "合成器", violin: "小提琴", strings: "弦乐组", flute: "长笛", bass: "电贝斯", drums: "鼓组", organ: "风琴", harp: "竖琴", trumpet: "小号", saxophone: "萨克斯", choir: "合唱垫" };

async function api(path, options) {
  const method = (options?.method || "GET").toUpperCase();
  const trace = path.startsWith("/creative-conversations");
  const startedAt = performance.now();
  if (trace) console.info("[music-agent] request start", { method, path });
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (trace) console.error("[music-agent] request network failure", {
      method, path, elapsed_ms: Math.round(performance.now() - startedAt),
      error_name: error.name, error_message: error.message,
    });
    throw error;
  }
  if (trace) console.info("[music-agent] response headers", {
    method, path, status: response.status,
    request_id: response.headers.get("X-Request-ID"),
    elapsed_ms: Math.round(performance.now() - startedAt),
  });
  if (response.status === 204) return null;
  const body = await response.text();
  let data;
  try {
    data = body ? JSON.parse(body) : {};
  } catch {
    data = { detail: body || `服务器返回了无法识别的响应（${response.status}）` };
  }
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map(item => item.msg).join("；")
      : data.detail;
    if (trace) console.error("[music-agent] response error", { method, path, status: response.status, detail });
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return data;
}

function showCreativeMessage(role, content) {
  const message = document.createElement("p");
  message.className = `chat-message ${role}`;
  message.textContent = content;
  creativeMessages.append(message);
  creativeMessages.scrollTop = creativeMessages.scrollHeight;
}

function applyBrief() {
  if (!latestBrief) return;
  form.elements.theme.value = latestBrief.theme;
  form.elements.style.value = latestBrief.style;
  form.elements["duration-mode"].value = latestBrief.duration_seconds == null ? "auto" : "fixed";
  if (latestBrief.duration_seconds != null) form.elements.duration.value = latestBrief.duration_seconds;
  form.elements.mode.value = "manual";
  form.elements.lyrics.value = latestBrief.lyrics || "";
  updateLyricsField();
  form.scrollIntoView({ behavior: "smooth", block: "start" });
  submitStatus.textContent = "已带入确认歌词；请检查或修改歌词后，再点击“生成音乐”。";
}

function renderConversation(messages) {
  creativeMessages.replaceChildren();
  if (!messages.length) return showCreativeMessage("assistant", assistantGreeting);
  messages.forEach(message => showCreativeMessage(message.role, message.content));
}

async function loadConversationList(selectId = null) {
  const conversations = await api("/creative-conversations");
  conversationList.replaceChildren();
  conversations.forEach(item => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.title || "新对话";
    conversationList.append(option);
  });
  if (!conversations.length) return createConversation();
  const target = conversations.find(item => item.id === selectId)?.id || conversations[0].id;
  conversationList.value = target;
  await selectConversation(target);
}

async function selectConversation(id) {
  const conversation = await api(`/creative-conversations/${id}`);
  activeConversation = conversation.id;
  chatHistory = conversation.messages || [];
  latestBrief = null;
  useBrief.hidden = true;
  previewBrief.hidden = true;
  previewHint.hidden = true;
  renderConversation(chatHistory);
}

async function createConversation() {
  const conversation = await api("/creative-conversations", { method: "POST" });
  activeConversation = conversation.id;
  chatHistory = [];
  latestBrief = null;
  useBrief.hidden = true;
  previewBrief.hidden = true;
  previewHint.hidden = true;
  const option = document.createElement("option");
  option.value = conversation.id;
  option.textContent = conversation.title;
  conversationList.prepend(option);
  conversationList.value = conversation.id;
  renderConversation([]);
}

conversationList.addEventListener("change", () => selectConversation(conversationList.value).catch(err => { creativeStatus.textContent = err.message; }));
newConversation.addEventListener("click", () => createConversation().catch(err => { creativeStatus.textContent = err.message; }));
deleteConversation.addEventListener("click", async () => {
  if (!activeConversation) return;
  if (!window.confirm("删除这条创作对话及其聊天记录？")) return;
  deleteConversation.disabled = true;
  try {
    await api(`/creative-conversations/${activeConversation}`, { method: "DELETE" });
    await loadConversationList();
  } catch (err) {
    creativeStatus.textContent = err.message;
  } finally {
    deleteConversation.disabled = false;
  }
});
chatSubmit.disabled = true;
loadConversationList().then(() => { chatSubmit.disabled = false; }).catch(err => { creativeStatus.textContent = err.message; });

function updateCreativeCapabilities() {
  const supportsWebSearch = creativeModel.value === "deepseek-flash";
  webSearch.disabled = !supportsWebSearch;
  if (!supportsWebSearch) {
    webSearch.checked = false;
    creativeStatus.textContent = "DeepSeek V4 Pro 可用于创作推演。联网搜索请切换到 DeepSeek Flash。";
  } else if (creativeStatus.textContent.startsWith("DeepSeek V4 Pro")) {
    creativeStatus.textContent = "";
  }
}

creativeModel.addEventListener("change", updateCreativeCapabilities);

creativeChat.addEventListener("submit", async event => {
  event.preventDefault();
  const input = creativeChat.elements.message;
  const content = input.value.trim();
  const button = creativeChat.querySelector("button");
  if (!content || button.disabled) return;
  latestBrief = null;
  useBrief.hidden = true;
  previewBrief.hidden = true;
  previewHint.hidden = true;
  chatHistory.push({ role: "user", content });
  showCreativeMessage("user", content);
  input.value = "";
  button.disabled = true;
  conversationList.disabled = newConversation.disabled = deleteConversation.disabled = true;
  creativeStatus.textContent = "正在整理创作想法…";
  const assistantMessage = document.createElement("p");
  assistantMessage.className = "chat-message assistant";
  assistantMessage.textContent = "";
  creativeMessages.append(assistantMessage);
  const requestStartedAt = performance.now();
  let requestStage = "chat-fetch";
  let receivedDone = false;
  try {
    const response = await fetch("/creative-chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: chatHistory.slice(-20),
        session_id: activeConversation,
        chat_type: "song",
        model: creativeModel.value,
        deep_think: deepThink.checked,
        web_search: webSearch.checked,
      }),
    });
    console.info("[music-agent] chat response headers", {
      status: response.status,
      request_id: response.headers.get("X-Request-ID"),
      elapsed_ms: Math.round(performance.now() - requestStartedAt),
    });
    if (!response.ok) {
      const data = await response.json();
      const detail = Array.isArray(data.detail) ? data.detail.map(item => item.msg).join("；") : data.detail;
      throw new Error(detail || `请求失败（${response.status}）`);
    }
    requestStage = "chat-stream-read";
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "";
    let reply = "";
    let brief = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      pending += decoder.decode(value, { stream: true });
      const events = pending.split("\n\n");
      pending = events.pop();
      for (const event of events) {
        const line = event.split("\n").find(item => item.startsWith("data: "));
        if (!line) continue;
        const message = JSON.parse(line.slice(6));
        if (message.type === "delta") {
          reply += message.text;
          assistantMessage.textContent = reply;
          creativeMessages.scrollTop = creativeMessages.scrollHeight;
          creativeStatus.textContent = "正在生成回复…";
        } else if (message.type === "done") {
          brief = message.brief;
          receivedDone = true;
          console.info("[music-agent] chat stream done", {
            elapsed_ms: Math.round(performance.now() - requestStartedAt),
          });
        } else if (message.type === "error") {
          console.error("[music-agent] chat stream error event", {
            elapsed_ms: Math.round(performance.now() - requestStartedAt),
          });
          throw new Error(message.message);
        }
      }
    }
    chatHistory.push({ role: "assistant", content: reply });
    latestBrief = brief;
    useBrief.hidden = !latestBrief || !latestBrief.lyrics;
    previewBrief.hidden = useBrief.hidden;
    previewHint.hidden = useBrief.hidden;
    requestStage = "conversation-refresh";
    console.info("[music-agent] chat finished; refreshing conversation", {
      received_done: receivedDone,
      elapsed_ms: Math.round(performance.now() - requestStartedAt),
    });
    await loadConversationList(activeConversation);
    creativeStatus.textContent = latestBrief ? "方案已准备好。" : "";
  } catch (error) {
    console.error("[music-agent] creative action failed", {
      stage: requestStage,
      received_done: receivedDone,
      elapsed_ms: Math.round(performance.now() - requestStartedAt),
      error_name: error.name,
      error_message: error.message,
      stack: error.stack,
    });
    if (!assistantMessage.textContent) assistantMessage.remove();
    creativeStatus.textContent = error.message;
  } finally {
    button.disabled = false;
    conversationList.disabled = newConversation.disabled = deleteConversation.disabled = false;
  }
});

useBrief.addEventListener("click", () => {
  applyBrief();
});

previewBrief.addEventListener("click", () => {
  if (!latestBrief?.lyrics) return;
  applyBrief();
  form.elements["duration-mode"].value = "fixed";
  form.elements.duration.value = "20";
  updateLyricsField();
  submitStatus.textContent = "正在提交 20 秒试听任务…";
  form.requestSubmit();
});

api("/creative-chat/config")
  .then(config => {
    creativeModel.value = config.model;
    updateCreativeCapabilities();
  })
  .catch(updateCreativeCapabilities);

trackSearch.addEventListener("submit", async event => {
  event.preventDefault();
  const query = new FormData(trackSearch).get("q").trim();
  const button = trackSearch.querySelector("button");
  if (!query || button.disabled) return;
  button.disabled = true;
  trackResults.replaceChildren();
  trackStatus.textContent = "正在搜索…";
  try {
    const tracks = await api("/tracks/search?" + new URLSearchParams({q: query}));
    for (const track of tracks) {
      const row = document.createElement("li");
      const title = document.createElement("p");
      title.textContent = [track.title, track.artist, track.album].filter(Boolean).join(" · ");
      row.append(title);
      if (track.playback_url) {
        const link = document.createElement("a");
        link.href = track.playback_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "在 Apple Music 打开";
        row.append(link);
      }
      trackResults.append(row);
    }
    trackStatus.textContent = tracks.length ? "" : "没有找到相关曲目。";
  } catch (error) {
    trackStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

function showJob(job) {
  const previous = jobs.get(job.id);
  if (previous && JSON.stringify(previous.job) === JSON.stringify(job)) return;

  const row = document.createElement("li");
  const title = document.createElement("strong");
  title.textContent = job.theme;
  const details = document.createElement("p");
  const arrangementLabels = { deepseek: "DeepSeek 编曲", local: "本地规则编曲" };
  const arrangement = job.instrumental && job.generator === "local" && job.status === "succeeded"
    ? arrangementLabels[job.arrangement_source] ?? "编曲来源未知"
    : "";
  const instrument = job.instrumental ? (Array.isArray(job.instrument) ? job.instrument : [job.instrument || "piano"]).map(name => instrumentLabels[name]).join(" + ") : "";
  const durationLabel = job.duration_seconds == null ? "自动时长" : `${job.duration_seconds} 秒`;
  details.textContent = [statuses[job.status] || job.status, job.generator, job.style, instrument, durationLabel, arrangement]
    .filter(Boolean).join(" · ");
  row.append(title, details);

  const projectSelect = document.createElement("select");
  projectSelect.setAttribute("aria-label", `选择加入的项目：${job.theme}`);
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "选择项目";
  projectSelect.append(placeholder);
  projects.forEach(project => {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.name;
    projectSelect.append(option);
  });
  projectSelect.value = activeProjectId || "";
  const addToProject = document.createElement("button");
  addToProject.type = "button";
  addToProject.className = "secondary";
  addToProject.textContent = "加入项目候选";
  addToProject.disabled = job.status !== "succeeded";
  addToProject.addEventListener("click", async () => {
    if (!projectSelect.value) { historyStatus.textContent = "请先创建并选择项目。"; return; }
    try {
      await api(`/music-projects/${projectSelect.value}/items/${job.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: "pending", note: "" }),
      });
      activeProjectId = projectSelect.value;
      await loadProjects();
      projectStatus.textContent = "候选作品已加入项目。";
    } catch (error) { historyStatus.textContent = error.message; }
  });
  const archive = document.createElement("button");
  archive.type = "button";
  archive.className = "secondary";
  archive.textContent = "确认归档到项目";
  archive.disabled = job.status !== "succeeded";
  archive.addEventListener("click", async () => {
    if (!projectSelect.value) { historyStatus.textContent = "请先创建并选择归档项目。"; return; }
    archive.disabled = true;
    try {
      await api(`/music-projects/${projectSelect.value}/items/${job.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: "selected", note: "用户确认归档" }),
      });
      activeProjectId = projectSelect.value;
      await loadProjects();
      projectStatus.textContent = `已确认归档第 ${job.version || 1} 版。可在项目中试听并下载入选作品包。`;
    } catch (error) { historyStatus.textContent = error.message; }
    finally { archive.disabled = false; }
  });
  row.append(projectSelect, addToProject, archive);

  if (job.generator === "ACE-Step" && ["queued", "running"].includes(job.status)) {
    const progressBox = document.createElement("div");
    progressBox.className = "generation-progress";
    const progressLabel = document.createElement("p");
    const progress = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
    progressLabel.textContent = job.status === "queued"
      ? "排队等待 ACE-Step"
      : `${job.progress_text || "等待 ACE-Step 返回进度"}${progress ? ` · ${progress}%` : ""}`;
    const progressBar = document.createElement("progress");
    progressBar.max = 100;
    progressBar.value = progress;
    progressBar.setAttribute("aria-label", `ACE-Step 生成进度 ${progress}%`);
    progressBox.append(progressLabel, progressBar);
    row.append(progressBox);
  } else if (job.instrumental && job.status === "running") {
    const progressNote = document.createElement("p");
    progressNote.className = "generation-progress-note";
    progressNote.textContent = "本地编曲与渲染中；当前生成器不提供百分比进度。";
    row.append(progressNote);
  }

  const tagEditor = document.createElement("div");
  tagEditor.className = "tag-editor";
  const tagInput = document.createElement("input");
  tagInput.type = "text";
  tagInput.maxLength = 400;
  tagInput.value = (job.tags || []).join(", ");
  tagInput.placeholder = "标签，用逗号分隔（如：Lo-fi、夜读、BPM 82）";
  tagInput.setAttribute("aria-label", `作品标签：${job.theme}`);
  const saveTags = document.createElement("button");
  saveTags.type = "button";
  saveTags.className = "secondary";
  saveTags.textContent = "保存标签";
  saveTags.addEventListener("click", async () => {
    saveTags.disabled = true;
    try {
      const tags = [...new Set(tagInput.value.split(",").map(tag => tag.trim()).filter(Boolean))];
      showJob(await api(`/generations/${job.id}/tags`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags }),
      }));
    } catch (error) {
      historyStatus.textContent = error.message;
    } finally {
      saveTags.disabled = false;
    }
  });
  tagEditor.append(tagInput, saveTags);
  row.append(tagEditor);

  if (job.audio_url) {
    const audio = document.createElement("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = job.audio_url;
    audio.setAttribute("aria-label", `试听 ${job.theme}`);
    row.append(audio);
  }

  if (job.status === "succeeded") {
    const revisionForm = document.createElement("form");
    revisionForm.className = "revision-form";
    const revisionLabel = document.createElement("label");
    revisionLabel.textContent = "告诉我这版要怎么调整";
    const feedback = document.createElement("textarea");
    feedback.name = "feedback";
    feedback.maxLength = 1000;
    feedback.required = true;
    feedback.rows = 3;
    feedback.placeholder = "例如：副歌更有力量；前奏缩短；人声更柔和。会重新生成整首新版本。";
    revisionLabel.append(feedback);
    const revise = document.createElement("button");
    revise.type = "submit";
    revise.textContent = "按意见生成新版本";
    revisionForm.append(revisionLabel);
    let revisedLyrics = null;
    if (!job.instrumental) {
      const lyricsLabel = document.createElement("label");
      lyricsLabel.textContent = "歌词（可选修改，留空则沿用当前歌词）";
      revisedLyrics = document.createElement("textarea");
      revisedLyrics.name = "lyrics";
      revisedLyrics.maxLength = 3000;
      revisedLyrics.rows = 5;
      revisedLyrics.value = job.lyrics || "";
      lyricsLabel.append(revisedLyrics);
      revisionForm.append(lyricsLabel);
    }
    revisionForm.append(revise);
    revisionForm.addEventListener("submit", async event => {
      event.preventDefault();
      revise.disabled = true;
      try {
        const next = await api(`/generations/${job.id}/revisions`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ feedback: feedback.value.trim(),
            lyrics: revisedLyrics && revisedLyrics.value.trim() !== job.lyrics ? revisedLyrics.value.trim() : null }),
        });
        showJob(next);
        historyStatus.textContent = `已提交第 ${next.version} 版，修改意见已记录。上一版仍保留，可继续比较。`;
        list.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) { historyStatus.textContent = error.message; }
      finally { revise.disabled = false; }
    });
    row.append(revisionForm);

    const versionButton = document.createElement("button");
    versionButton.type = "button";
    versionButton.className = "secondary";
    versionButton.textContent = "查看此作品的全部版本";
    const versionList = document.createElement("div");
    versionList.className = "version-list";
    versionButton.addEventListener("click", async () => {
      versionButton.disabled = true;
      try {
        const versions = await api(`/generations/${job.id}/versions`);
        versionList.replaceChildren();
        versions.forEach(version => {
          const item = document.createElement("article");
          const heading = document.createElement("strong");
          heading.textContent = `第 ${version.version} 版 · ${statuses[version.status] || version.status}${version.id === job.id ? "（当前）" : ""}`;
          item.append(heading);
          if (version.revision_feedback) {
            const note = document.createElement("p");
            note.textContent = `修改意见：${version.revision_feedback}`;
            item.append(note);
          }
          if (version.audio_url) {
            const player = document.createElement("audio");
            player.controls = true;
            player.preload = "none";
            player.src = version.audio_url;
            item.append(player);
          } else if (version.error) {
            const error = document.createElement("p");
            error.textContent = version.error;
            item.append(error);
          }
          versionList.append(item);
        });
      } catch (error) { historyStatus.textContent = error.message; }
      finally { versionButton.disabled = false; }
    });
    row.append(versionButton, versionList);
  }

  if (job.status === "failed") {
    const error = document.createElement("p");
    error.textContent = job.error || "";
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "secondary";
    retry.textContent = "重试原参数";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try {
        showJob(await api(`/generations/${job.id}/retry`, { method: "POST" }));
      } catch (err) {
        error.textContent = err.message;
      } finally {
        retry.disabled = false;
      }
    });
    row.append(error, retry);
  }

  if (previous) previous.row.replaceWith(row);
  else list.append(row);
  jobs.set(job.id, { job, row });
}

function renderProjects() {
  projectsElement.replaceChildren();
  for (const project of projects) {
    const section = document.createElement("section");
    section.className = "project-card";
    const heading = document.createElement("div");
    heading.className = "heading";
    const title = document.createElement("h3");
    title.textContent = project.name;
    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "secondary";
    choose.textContent = activeProjectId === project.id ? "当前项目" : "设为当前项目";
    choose.addEventListener("click", () => {
      activeProjectId = project.id;
      renderProjects();
      const existingJobs = [...jobs.values()].map(entry => entry.job);
      jobs.clear(); list.replaceChildren(); existingJobs.forEach(showJob);
    });
    heading.append(title, choose);
    section.append(heading);
    const stats = document.createElement("p");
    const selectedCount = project.items.filter(item => item.decision === "selected").length;
    stats.textContent = `${project.items.length} 个候选 · ${selectedCount} 个入选`;
    section.append(stats);
    if (!project.items.length) {
      const empty = document.createElement("p"); empty.textContent = "尚无候选作品。可在生成记录里加入已完成的作品。"; section.append(empty);
    }
    for (const item of project.items) {
      const row = document.createElement("article");
      row.className = "project-item";
      const name = document.createElement("strong"); name.textContent = item.job.theme;
      const audio = document.createElement("audio"); audio.controls = true; audio.preload = "none"; audio.src = item.job.audio_url;
      const controls = document.createElement("div"); controls.className = "fields";
      const decision = document.createElement("select");
      for (const [value, label] of [["pending", "待定"], ["selected", "入选"], ["rejected", "淘汰"]]) {
        const option = document.createElement("option"); option.value = value; option.textContent = label; decision.append(option);
      }
      decision.value = item.decision;
      const note = document.createElement("input"); note.maxLength = 500; note.placeholder = "筛选备注（可选）"; note.value = item.note || "";
      const save = document.createElement("button"); save.type = "button"; save.className = "secondary"; save.textContent = "保存筛选";
      save.addEventListener("click", async () => {
        save.disabled = true;
        try { await api(`/music-projects/${project.id}/items/${item.job.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: decision.value, note: note.value }) }); await loadProjects(); }
        catch (error) { projectStatus.textContent = error.message; }
        finally { save.disabled = false; }
      });
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary"; remove.textContent = "移出项目";
      remove.addEventListener("click", async () => { try { await api(`/music-projects/${project.id}/items/${item.job.id}`, { method: "DELETE" }); await loadProjects(); } catch (error) { projectStatus.textContent = error.message; } });
      controls.append(decision, note, save, remove);
      row.append(name, audio, controls);
      if (item.note) { const noteText = document.createElement("p"); noteText.textContent = `备注：${item.note}`; row.append(noteText); }
      section.append(row);
    }
    const exportButton = document.createElement("a");
    exportButton.className = "button-link"; exportButton.href = `/music-projects/${project.id}/export`; exportButton.textContent = "下载入选作品 ZIP";
    section.append(exportButton);
    projectsElement.append(section);
  }
}

async function loadProjects() {
  projects = await api("/music-projects");
  if (!projects.some(project => project.id === activeProjectId)) activeProjectId = projects[0]?.id || null;
  renderProjects();
  // Keep project choices in existing generation rows in sync with project creation.
  const existingJobs = [...jobs.values()].map(entry => entry.job);
  jobs.clear();
  list.replaceChildren();
  existingJobs.forEach(showJob);
}

projectCreate.addEventListener("submit", async event => {
  event.preventDefault();
  const button = projectCreate.querySelector("button"); button.disabled = true;
  try {
    const project = await api("/music-projects", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: new FormData(projectCreate).get("name").trim() }) });
    activeProjectId = project.id; projectCreate.reset(); projectStatus.textContent = "项目已创建。现在可将生成记录加入项目候选。"; await loadProjects();
  } catch (error) { projectStatus.textContent = error.message; }
  finally { button.disabled = false; }
});
refreshProjects.addEventListener("click", () => loadProjects().catch(error => { projectStatus.textContent = error.message; }));
loadProjects().catch(error => { projectStatus.textContent = error.message; });

async function loadHistory(after = null) {
  refresh.disabled = more.disabled = true;
  try {
    const params = new URLSearchParams({ limit: "20" });
    if (after) params.set("after", after);
    const page = await api(`/generations?${params}`);
    page.items.forEach(showJob);
    cursor = page.next_cursor;
    more.hidden = !cursor;
    historyStatus.textContent = jobs.size ? "" : "还没有生成记录。";
  } catch (err) {
    historyStatus.textContent = err.message;
  } finally {
    refresh.disabled = more.disabled = false;
  }
}

async function pollJobs() {
  for (const { job } of jobs.values()) {
    if (!["queued", "running"].includes(job.status)) continue;
    try {
      showJob(await api(`/generations/${job.id}`));
    } catch (err) {
      historyStatus.textContent = `状态更新失败，将自动重试。 ${err.message}`;
      return;
    }
  }
  if (historyStatus.textContent.startsWith("状态更新失败")) historyStatus.textContent = "";
}

const mode = form.elements.mode;
const vocalOptions = [...mode.options].filter(option => option.value !== "instrumental");
api("/generations/config").then(config => {
  const aceInstrumentalOption = form.elements.instrumental_generator.querySelector('option[value="ace-step"]');
  if (!config.ace_step_instrumental_available) {
    aceInstrumentalOption.disabled = true;
    if (form.elements.instrumental_generator.value === "ace-step") {
      form.elements.instrumental_generator.value = "local";
      updateLyricsField();
    }
  }
  if (!config.vocal_generation_available) {
    vocalOptions.forEach(option => { option.disabled = true; });
    const hint = document.createElement("p");
    hint.textContent = "当前无法连接 ACE-Step API。纯音乐可暂时选择本地程序合成；启动 ACE-Step 后即可使用模型生成纯音乐和人声歌曲。";
    hint.setAttribute("role", "status");
    document.querySelector("#lyrics-field").before(hint);
  }
}).catch(() => {});
function updateLyricsField() {
  const instrumental = mode.value === "instrumental";
  const localInstrumental = instrumental && form.elements.instrumental_generator.value === "local";
  const automaticDurationOption = form.elements["duration-mode"].querySelector('option[value="auto"]');
  automaticDurationOption.disabled = instrumental;
  if (instrumental && form.elements["duration-mode"].value === "auto") {
    form.elements["duration-mode"].value = "fixed";
  }
  document.querySelector("#lyrics-field").hidden = mode.value !== "manual";
  document.querySelector("#instrumental-generator-field").hidden = !instrumental;
  document.querySelector("#instrument-field").hidden = !instrumental;
  document.querySelector("#arrangement-model-field").hidden = !localInstrumental;
  document.querySelector("#reference-audio-field").hidden = instrumental;
  document.querySelector("#mix-audio-field").hidden = !localInstrumental;
  document.querySelector("#mix-controls").hidden = !localInstrumental;
  const autoDuration = form.elements["duration-mode"].value === "auto" && !instrumental;
  document.querySelector("#duration-seconds-field").hidden = autoDuration;
  form.elements.duration.required = !autoDuration;
  submitStatus.textContent = "";
}
mode.addEventListener("change", updateLyricsField);
form.elements.instrumental_generator.addEventListener("change", updateLyricsField);
form.elements["duration-mode"].addEventListener("change", updateLyricsField);
updateLyricsField();

form.addEventListener("submit", async event => {
  event.preventDefault();
  const fields = new FormData(form);
  const theme = fields.get("theme").trim();
  if (!theme) {
    submitStatus.textContent = "请填写音乐主题。";
    return;
  }
  if (fields.get("mode") === "manual" && !fields.get("lyrics").trim()) {
    submitStatus.textContent = "人声歌曲需要先在上方创作并确认歌词。";
    return;
  }

  const button = form.querySelector("button");
  button.disabled = true;
  submitStatus.textContent = "正在提交…";
  try {
    const job = await api("/generations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        theme,
        style: fields.get("style").trim(),
        instrument: fields.getAll("instrument"),
        focus_instrument: fields.get("focus_instrument"),
        support_level: fields.get("support_level"),
        arrangement_model: fields.get("arrangement_model"),
        lyrics: fields.get("mode") === "manual" ? fields.get("lyrics").trim() : "",
        instrumental: fields.get("mode") === "instrumental",
        instrumental_generator: fields.get("mode") === "instrumental" ? fields.get("instrumental_generator") : "local",
        draft_lyrics: false,
        duration_seconds: fields.get("duration-mode") === "auto" && fields.get("mode") === "manual"
          ? null
          : Number(fields.get("duration")),
        reference_audio_asset_id: fields.get("mode") === "manual" ? fields.get("reference_audio_asset_id") || null : null,
        mix_asset_ids: fields.get("mode") === "instrumental" && fields.get("instrumental_generator") === "local" ? [...selectedMixAssetIds] : [],
        mix_gain: Number(fields.get("mix_gain") || 0.25),
        mix_start_seconds: Number(fields.get("mix_start_seconds") || 0),
      }),
    });
    showJob(job);
    list.prepend(jobs.get(job.id).row);
    historyStatus.textContent = "";
    submitStatus.textContent = "任务已提交。";
  } catch (err) {
    submitStatus.textContent = err.message;
  } finally {
    button.disabled = false;
  }
});

refresh.addEventListener("click", () => loadHistory());
more.addEventListener("click", () => loadHistory(cursor));
loadHistory();
async function poll() {
  await pollJobs();
  setTimeout(poll, 3000);
}
setTimeout(poll, 3000);
