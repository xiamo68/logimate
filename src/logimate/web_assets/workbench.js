const $ = id => document.getElementById(id);
const labels = {
  answered: "已作答", partial: "部分证据", needs_clarification: "需要补充",
  queued: "等待执行", running: "正在执行", interrupted: "已中断",
  completed: "已完成", failed: "失败", pending: "待复核", verified: "已复核", rejected: "已驳回",
  draft: "草稿", replaying: "回放中", passed: "回放通过", approved: "已批准", released: "已发布",
  initial: "初始版本", base: "基础层", meta: "元层", replay: "重新回放",
  order: "订单", warehouse: "仓储", logistics: "物流", aftersales: "售后", policy: "政策",
  intent: "识别意图", compliance: "校验权限", plan: "安排查询", conclusion: "汇总证据",
  "order.get": "查询订单", "warehouse.get_events": "查询仓储",
  "logistics.get_tracking": "查询物流", "aftersales.get_cases": "查询售后",
  not_found: "未找到记录", unavailable: "来源暂不可用", timeout: "调用超时",
  active_baseline: "对比活动版本", skill_ablation: "同起点技能对照",
  hybrid: "混合检索", keyword_only: "关键词回退", semantic_only: "语义检索", demo: "无密钥",
  deepseek: "DeepSeek", rag: "售后检索", grounding: "事实校验",
  status: "状态", promised_by: "承诺送达", handoff_recorded: "有交接记录",
  marked_shipped_at: "标记发货", outbound_at: "出库时间", latest_event_at: "最新记录",
  case_count: "工单数", label_created: "已创建面单", picked_up: "已揽收", shipped: "已发货",
};
const diagnosisLabels = {
  normal: "正常", awaiting_handoff: "等待交接", awaiting_pickup: "等待揽收", pickup_overdue: "揽收超时",
  in_transit_delayed: "运输延误", delivery_disputed: "签收争议", needs_verification: "待核实",
};
const viewLabels = {investigate: "排查", improve: "策略改进", evaluate: "评测"};
const emptyResult = "输入订单号后查看核查结论和业务证据。";
const emptyAction = "查询后将根据已取得的证据给出处理建议。";
const stages = {order: "订单状态", warehouse: "仓储出库与交接", logistics: "承运商揽收", aftersales: "售后工单", policy: "适用政策"};
const handling = {
  awaiting_handoff: ["联系仓库核实交接", "核对交接单、交接时间与包裹号。若尚未交接，请仓库确认预计交接时间。"],
  awaiting_pickup: ["向承运商核实揽收进度", "提供包裹号及交接凭证，确认是否完成首次揽收扫描。"],
  pickup_overdue: ["向承运商查询揽收异常", "提交交接凭证与包裹号，确认实物位置和预计揽收时间。"],
  in_transit_delayed: ["向承运商确认送达时间", "核对最新扫描和承诺送达时间，确认延误原因。"],
  delivery_disputed: ["转售后核实签收争议", "收集签收凭证、运单号和用户反馈。"],
  needs_verification: ["补齐缺失的履约记录", "联系对应业务方补齐证据后重新查询。"],
  normal: ["按当前履约节点跟进", "若客户反馈与业务记录不一致，补充具体问题后再核查。"],
};
const retryKeys = new Map();
let actor = null;
let activeView = "investigate";
let identityGeneration = 0;
let observation = 0;
let switching = false;
let writeCount = 0;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function show(parent, ...items) {
  parent.replaceChildren(...items);
}

function announce(message, error = false) {
  $("notice").textContent = message;
  $("notice").dataset.tone = error ? "error" : "success";
  $("notice").hidden = !message;
}

function local(name, value) {
  const key = `logimate:${actor.profile}:${name}`;
  if (value === undefined) return localStorage.getItem(key);
  if (value === null) localStorage.removeItem(key);
  else localStorage.setItem(key, value);
}

function working() {
  return writeCount > 0 || switching;
}

async function api(path, method = "GET", body = null) {
  const headers = {Accept: "application/json"};
  const payload = body !== null ? JSON.stringify(body) : undefined;
  if (payload !== undefined) headers["Content-Type"] = "application/json";
  const retries = method !== "GET" && path !== "/demo/sign-in";
  const attempt = retries ? `${actor.profile}:${method}:${path}:${payload ?? ""}` : null;
  if (attempt) {
    if (!retryKeys.has(attempt)) retryKeys.set(attempt, crypto.randomUUID());
    headers["Idempotency-Key"] = retryKeys.get(attempt);
  }
  let response;
  try {
    response = await fetch("/api/v1" + path, {method, headers, body: payload});
  } catch {
    throw new Error("无法连接服务，请检查本地应用后重试。");
  }
  if (attempt && response.status < 500) retryKeys.delete(attempt);
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(data?.message || `请求失败（HTTP ${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function action(button, perform) {
  if (!actor || working() || button.disabled) return;
  writeCount++;
  button.disabled = true;
  $("switch-profile").disabled = true;
  Promise.resolve().then(perform).catch(error => announce(error.message, true)).finally(() => {
    writeCount--;
    button.disabled = false;
    $("switch-profile").disabled = switching || writeCount > 0;
  });
}

function control(text, callback, className = "quiet") {
  const button = node("button", text, className);
  button.type = "button";
  button.addEventListener("click", () => action(button, callback));
  return button;
}

function textLabel(title, controlElement) {
  const label = node("label", title, "form-label");
  label.append(controlElement);
  return label;
}

function shortId(id) {
  return id && id.length > 18 ? id.slice(0, 12) + "…" : id;
}

function evidenceRow(item) {
  const details = node("details", undefined, "evidence-row");
  const summary = node("summary");
  const overview = item.policy
    ? item.policy.title_path.join(" / ")
    : item.source === "warehouse" && item.fields.some(field => field.name === "handoff_recorded" && field.value === false)
      ? "仓储查询未见交接记录"
    : item.fields.filter(field => field.value !== null).map(
      field => `${labels[field.name] || field.name} ${labels[field.value] || translatedValue(field.value)}`,
    ).join(" · ");
  summary.append(node("strong", stages[item.source] || item.source), node("span", overview || item.resource_id),
                 node("span", "原始记录 ›"));
  details.append(summary);
  const body = node("div", undefined, "detail-body");
  if (item.policy) {
    body.append(node("p", item.policy.excerpt), node("p", `政策版本 ${item.policy.version} · 生效 ${item.policy.effective_at}`, "metadata"));
  }
  if (item.fields.length) {
    const fields = node("dl", undefined, "record-fields");
    for (const {name, value} of item.fields.filter(field => field.value !== null)) {
      fields.append(node("dt", labels[name] || name), node("dd", labels[value] || translatedValue(value)));
    }
    body.append(fields);
  }
  body.append(node("p", `记录 ${item.resource_id} · 查询时点 ${item.observed_at}`, "metadata"),
              node("p", `证据 ID ${item.evidence_id}`, "metadata"));
  details.append(body);
  return details;
}

function translatedValue(value) {
  if (value === true) return "是";
  if (value === false) return "否";
  if (typeof value === "string" && /^\d{4}-\d\d-\d\dT/.test(value)) {
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) return date.toLocaleString("zh-CN", {hour12: false});
  }
  return String(value);
}

function resolveHandling(details, evidence) {
  const codes = [...new Set(details.diagnoses.map(item => item.classification))];
  const warehouse = evidence.filter(item => item.source === "warehouse");
  const tracking = evidence.filter(item => item.source === "logistics");
  const labelOnly = tracking.length === 1 && tracking[0].fields.some(field =>
    field.name === "status" && ["label_created", "no_events"].includes(field.value));
  const missingHandoff = warehouse.length === 1 && warehouse[0].fields.some(field =>
    field.name === "handoff_recorded" && field.value === false);
  if (details.status === "answered" && details.retrieval_mode && evidence.some(item => item.policy)) {
    return {title: "已查到相关政策", summary: "展开适用政策查看条款原文。",
      step: ["按适用条款核对处理条件", "结合订单状态和政策原文，再决定是否转售后。"], warning: false};
  }
  if (codes.length === 1 && ["pickup_overdue", "awaiting_handoff"].includes(codes[0]) && missingHandoff && labelOnly) {
    return {
      title: codes[0] === "awaiting_handoff" ? "等待仓储交接" : "待核实仓储交接",
      summary: "仓储未见交接记录，物流尚无揽收。请核实实物是否已移交。",
      step: handling.awaiting_handoff, warning: true,
    };
  }
  if (details.status !== "answered") {
    return {title: labels[details.status], summary: details.summary,
      step: details.status === "needs_clarification" ? ["补充订单号或问题", "提供明确的订单号，或打开追问补充上下文。"] :
        handling.needs_verification, warning: true};
  }
  const code = codes[0];
  return {title: codes.map(item => diagnosisLabels[item]).join(" / ") || "已完成核查",
    summary: "以下是本次查询取得的业务记录。", step: codes.length > 1
      ? ["逐个核对包裹状态", "各包裹的履约状态不同，请分别核对运单和联系对应业务方。"]
      : handling[code] || handling.needs_verification, warning: code !== "normal"};
}

function normalizeEvidence(evidence) {
  const rank = ["order", "warehouse", "logistics", "aftersales", "policy"];
  return evidence.toSorted((a, b) => rank.indexOf(a.source) - rank.indexOf(b.source));
}

function showTrace(events) {
  if (!events.length) {
    show($("trace"), node("p", "当前无调用轨迹。", "empty"));
    return;
  }
  const timeline = node("ol", undefined, "timeline");
  const tools = events.filter(event => event.kind === "tool");
  const ordered = [
    ...events.filter(event => ["intent", "compliance", "plan"].includes(event.node_id)),
    ...tools,
    ...events.filter(event => event.node_id === "conclusion"),
  ];
  for (const event of ordered) {
    const entry = node("li");
    entry.dataset.state = event.status;
    entry.append(node("strong", labels[event.tool_name || event.node_id] || event.node_id),
                 node("p", `${event.duration_ms} ms${event.attempt ? ` · 第 ${event.attempt} 次` : ""}` +
                   (event.error_code ? ` · ${labels[event.error_code] || event.error_code}` : "")));
    timeline.append(entry);
  }
  show($("trace"), timeline);
}

function feedbackForm(run, evidence) {
  const eligible = evidence.filter(item => ["warehouse", "logistics"].includes(item.source));
  if (actor.role !== "operator" || new Set(eligible.map(item => item.source)).size < 2) return;
  const details = node("details", undefined, "block");
  details.append(node("summary", "提交运营纠正"));
  const form = node("form");
  const options = node("select");
  options.name = "classification";
  options.required = true;
  for (const [key, caption] of Object.entries(diagnosisLabels)) options.add(new Option(caption, key));
  options.value = "awaiting_handoff";
  const reason = node("textarea");
  reason.name = "reason";
  reason.required = true;
  reason.maxLength = 2000;
  reason.rows = 2;
  const send = node("button", "提交纠正", "primary");
  send.type = "submit";
  form.append(textLabel("正确分类", options), textLabel("纠正依据", reason),
              node("p", "将引用本次排查的仓储与物流证据。", "form-help"), send);
  form.addEventListener("submit", event => {
    event.preventDefault();
    action(send, async () => {
      await api(`/runs/${run.run_id}/feedback`, "POST", {
        corrected_classification: options.value, reason: reason.value.trim(),
        evidence_ids: eligible.map(item => item.evidence_id),
      });
      show(details, node("summary", "运营纠正已提交"), node("p", "等待审核员复核。", "empty"));
      announce("纠正已提交，等待审核员复核。");
    });
  });
  details.append(form);
  $("feedback-form-slot").append(details);
}

async function showRun(run, token, generation) {
  if (generation !== identityGeneration || token !== observation) return;
  const body = $("result");
  show($("feedback-form-slot"));
  if (run.status !== "completed" || !run.result) {
    show(body, node("h2", "订单排查结果"), node("p", run.error?.message || labels[run.status] || run.status, "empty"));
    show($("next-action"), node("p", emptyAction, "empty"));
    if (run.status === "interrupted") {
      body.append(control("继续排查", async () => {
        await api(`/runs/${run.run_id}/resume`, "POST");
        await watchRun(run.run_id);
      }, "primary"));
    }
    return;
  }
  const [trace, evidence] = await Promise.all([
    api(`/runs/${run.run_id}/trace`),
    Promise.all(run.result.evidence_ids.map(id => api(`/evidence/${id}`))),
  ]);
  if (generation !== identityGeneration || token !== observation) return;
  const details = run.result;
  const name = [...new Set(details.diagnoses.map(item => diagnosisLabels[item.classification]))].join(" / ");
  const status = resolveHandling(details, evidence);
  const order = evidence.find(item => item.source === "order");
  const heading = node("div", undefined, "section-head");
  heading.append(node("h2", order ? `订单 ${order.resource_id}` : "排查结果"));
  if (details.status === "answered") heading.append(node("span", "核查完成", "result-status"));
  const banner = node("div", undefined, "diagnosis");
  if (status.warning) banner.dataset.tone = "warning";
  banner.append(node("strong", status.title), node("p", status.summary));
  show(body, heading, banner);
  const shortcut = node("button", status.step[0] + " ›", "mobile-action");
  shortcut.type = "button";
  shortcut.addEventListener("click", () => $("handling-panel").focus());
  body.append(shortcut);
  const actionBox = node("div", undefined, "action-box");
  actionBox.append(node("h3", status.step[0]), node("p", status.step[1]));
  show($("next-action"), actionBox);
  if (details.missing_information.length) {
    $("next-action").append(node("p", "待补充 " + details.missing_information.join("、"), "subtle"));
  }
  if (evidence.length) {
    const list = node("div", undefined, "evidence-list");
    const tableHeading = node("div", undefined, "evidence-header");
    tableHeading.append(node("span", "业务环节"), node("span", "核查结果"), node("span", "详情"));
    list.append(tableHeading, ...normalizeEvidence(evidence).map(evidenceRow));
    body.append(node("h3", "业务证据"), list);
  }
  if (details.next_steps.length) $("next-action").append(node("p", details.next_steps.join(" · "), "subtle"));
  showTrace(trace.events);
  $("trace").prepend(node("p", `规则结论：${name || "无"} · 版本 ${shortId(run.versions.version_id)} · ${labels[run.mode] || run.mode}` +
    (details.retrieval_mode ? ` · ${labels[details.retrieval_mode]}` : ""), "metadata"));
  feedbackForm(run, evidence);
}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function watchRun(id) {
  const token = ++observation;
  const generation = identityGeneration;
  local("run", id);
  show($("trace"), node("p", "等待本次调用轨迹。", "empty"));
  $("technical").open = false;
  for (let index = 0; index < 110; index++) {
    if (token !== observation || generation !== identityGeneration) return;
    const run = await api(`/runs/${id}`);
    await showRun(run, token, generation);
    if (!["queued", "running"].includes(run.status)) {
      if (generation === identityGeneration) await loadHistory();
      return;
    }
    await delay(950);
  }
  announce("排查尚未结束，可刷新页面查看原运行。");
}

async function loadHistory() {
  const id = actor && local("session");
  const generation = identityGeneration;
  if (!id) return;
  const history = await api(`/sessions/${id}?limit=100`);
  if (generation !== identityGeneration) return;
  const questions = history.messages.filter(item => item.role === "user").reverse();
  $("history-panel").hidden = !questions.length;
  show($("history"), ...questions.map(question => {
    const row = node("div", undefined, "block row");
    row.append(control(question.content, () => watchRun(question.run_id)),
               node("span", labels[history.runs.find(run => run.run_id === question.run_id)?.status] || "", "metadata"));
    return row;
  }));
}

async function signIn(profile) {
  if (working()) return;
  switching = true;
  $("switch-profile").disabled = true;
  $("ask").disabled = true;
  const generation = ++identityGeneration;
  ++observation;
  try {
    const next = await api("/demo/sign-in", "POST", {profile});
    actor = next;
    localStorage.setItem("logimate:profile", profile);
    $("profile").value = profile;
    show($("connection"), node("span", undefined, "dot"), document.createTextNode("会话：已连接"));
    resetRunPanel();
    show($("feedback-list"));
    show($("versions"));
    show($("job-status"));
    await refreshView();
    const last = local("run");
    if (last && generation === identityGeneration && activeView === "investigate") {
      watchRun(last).catch(error => {
        if (generation === identityGeneration) announce(error.message, true);
      });
    }
    const job = local("job");
    if (job && activeView === "improve") {
      watchJob(job, ++jobObservation).catch(error => {
        if (generation === identityGeneration) announce(error.message, true);
      });
    }
  } catch (error) {
    show($("connection"), document.createTextNode(actor ? "会话：保持原身份" : "会话：未连接"));
    if (actor) $("profile").value = actor.profile;
    announce(error.message, true);
  } finally {
    switching = false;
    $("switch-profile").disabled = false;
    $("ask").disabled = !actor;
  }
}

function resetRunPanel() {
  show($("result"), node("h2", "订单排查结果"), node("p", emptyResult, "empty"));
  show($("next-action"), node("p", emptyAction, "empty"));
  $("technical").open = false;
  show($("trace"), node("p", "等待排查。", "empty"));
  show($("feedback-form-slot"));
  $("history-panel").hidden = true;
  show($("history"));
}

$("identity-form").addEventListener("submit", event => {
  event.preventDefault();
  void signIn($("profile").value);
});

$("sample-order").addEventListener("click", () => {
  $("order-number").value = "LM-1001";
  $("order-number").focus();
});

$("investigate-form").addEventListener("submit", event => {
  event.preventDefault();
  const followUp = event.submitter === $("ask-follow-up");
  const field = followUp ? $("question") : $("order-number");
  const value = field.value.trim();
  if (!value) {
    field.focus();
    return announce(followUp ? "请输入要追问的问题。" : "请输入订单号。", true);
  }
  if (!followUp && !/^LM-[a-z0-9][a-z0-9-]{0,55}$/i.test(value)) {
    field.focus();
    return announce("订单号格式不正确，例如 LM-1001。", true);
  }
  const question = followUp ? value : `查询订单 LM-${value.slice(3)}`;
  action(followUp ? $("ask-follow-up") : $("ask"), async () => {
    announce("");
    let session = local("session");
    if (!session) {
      session = (await api("/sessions", "POST", {})).session_id;
      local("session", session);
    }
    const run = await api(`/sessions/${session}/runs`, "POST", {question});
    await watchRun(run.run_id);
  });
});

$("new-session").addEventListener("click", () => {
  if (!actor || working()) return;
  ++observation;
  local("session", null);
  local("run", null);
  resetRunPanel();
  announce("下一次提问会创建新会话。");
  $("order-number").focus();
});

async function refreshView() {
  if (!actor) return;
  try {
    if (activeView === "improve") await loadImprove();
    if (activeView === "evaluate") await loadEvaluations();
    if (activeView === "investigate") await loadHistory();
  } catch (error) {
    announce(error.message, true);
  }
}

function selectView() {
  const hash = location.hash.slice(1);
  activeView = Object.hasOwn(viewLabels, hash) ? hash : "investigate";
  for (const view of Object.keys(viewLabels)) $(view).hidden = view !== activeView;
  for (const link of document.querySelectorAll(".tabs a")) {
    if (link.dataset.view === activeView) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  announce("");
  void refreshView();
}

window.addEventListener("hashchange", selectView);

function feedbackCard(item) {
  const card = node("article", undefined, "block");
  card.append(node("h3", `${diagnosisLabels[item.corrected_classification]} · ${labels[item.status]}`),
              node("p", item.reason), node("p", `提交人 ${item.submitted_by}`, "metadata"));
  if (item.review_reason) card.append(node("p", item.review_reason, "subtle"));
  if (actor.role === "operator" && item.status === "verified") {
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.name = "feedback";
    checkbox.value = item.feedback_id;
    checkbox.checked = true;
    const label = node("label", " 用于基础层改进", "metadata");
    label.prepend(checkbox);
    card.append(label);
  }
  if (actor.role === "reviewer") {
    const citations = node("details");
    citations.append(node("summary", "核对引用证据"));
    citations.addEventListener("toggle", () => {
      if (!citations.open || citations.dataset.loaded) return;
      citations.dataset.loaded = "true";
      Promise.all(item.evidence_ids.map(id => api(`/evidence/${id}`))).then(items => {
        citations.append(...items.map(evidenceRow));
      }).catch(error => {
        delete citations.dataset.loaded;
        announce(error.message, true);
      });
    });
    card.append(citations);
    if (item.status !== "pending") return card;
    const reason = node("input");
    reason.maxLength = 2000;
    reason.required = true;
    const actions = node("div", undefined, "row");
    const send = async decision => {
      if (!reason.value.trim()) {
        reason.focus();
        throw new Error("请填写复核理由。");
      }
      await api(`/feedback/${item.feedback_id}/verify`, "POST", {decision, reason: reason.value.trim()});
      announce(decision === "approve" ? "纠正已复核。" : "纠正已驳回。");
      await loadImprove();
    };
    actions.append(control("通过复核", () => send("approve"), "primary"), control("驳回纠正", () => send("reject")));
    card.append(textLabel("复核理由", reason), actions);
  }
  return card;
}

async function submitJob(body) {
  const buttonText = labels[body.kind];
  const job = await api("/evolution/jobs", "POST", body);
  local("job", job.job_id);
  show($("job-status"), node("p", `${buttonText}任务已提交，等待 worker。`, "empty"));
  await watchJob(job.job_id, ++jobObservation);
}

let jobObservation = 0;

async function watchJob(jobId, token) {
  const generation = identityGeneration;
  for (let index = 0; index < 70; index++) {
    if (generation !== identityGeneration || token !== jobObservation) return;
    const job = await api(`/evolution/jobs/${jobId}`);
    if (generation !== identityGeneration || token !== jobObservation) return;
    show($("job-status"), node("p", `${labels[job.kind]}任务 · ${labels[job.status]}` +
      (job.total_cases ? ` · ${job.processed_cases}/${job.total_cases} 个案例` : ""), "block"),
      ...(job.error ? [node("p", job.error.message, "empty")] : []));
    if (["completed", "failed"].includes(job.status)) {
      local("job", null);
      if (activeView === "improve") await loadImprove();
      return;
    }
    await delay(950);
  }
  announce("任务仍在队列中。确认 worker 已启动，再刷新查看进度。");
}

function replayTable(report) {
  const detail = node("details", undefined, "block");
  const score = `${report.baseline_error_count} → ${report.candidate_error_count} 个错误 / ${report.sample_count} 例`;
  detail.append(node("summary", `${labels[report.comparison]} · ${score} · ${report.passed ? "通过" : "未通过"}`),
                node("p", `新增重点错误 ${report.added_critical_errors} · 数据 ${report.data_version} · ${labels[report.mode]}`, "metadata"));
  const table = node("table", undefined, "table");
  const head = table.createTHead().insertRow();
  for (const title of ["案例", "预期", "基线", "候选"]) head.append(node("th", title));
  const tbody = table.createTBody();
  for (const item of report.examples) {
    const row = tbody.insertRow();
    for (const value of [item.case_id, ...["expected", "baseline", "candidate"].map(
      key => diagnosisLabels[item[key]] || item[key] || "—")]) row.append(node("td", value));
  }
  detail.append(table);
  return detail;
}

function versionControls(detail, active) {
  const version = detail.version;
  const id = version.version_id;
  const actions = node("div", undefined, "block");
  if (actor.role === "operator") {
    if (["passed", "failed", "approved", "rejected"].includes(version.state)) {
      actions.append(control("重新回放", () => submitJob({kind: "replay", version_id: id})));
    }
    return actions;
  }
  const report = detail.reports.find(item => item.comparison === "active_baseline" &&
    item.baseline_release_id === active.release_id && item.passed);
  if (version.state === "approved" && report) {
    actions.append(control("发布候选", async () => {
      await api(`/versions/${id}/release`, "POST", {expected_release_id: active.release_id});
      announce("候选已发布，新排查将使用此版本。");
      await loadImprove();
    }, "primary"));
  } else if (version.state === "passed" && report) {
    const reason = node("input");
    reason.maxLength = 2000;
    const review = async decision => {
      if (!reason.value.trim()) {
        reason.focus();
        throw new Error("请填写候选审核理由。");
      }
      await api(`/versions/${id}/review`, "POST", {decision, reason: reason.value.trim(), report_id: report.report_id});
      announce(decision === "approve" ? "候选已批准，可以发布。" : "候选已驳回。");
      await loadImprove();
    };
    const row = node("div", undefined, "row");
    row.append(control("批准候选", () => review("approve"), "primary"), control("驳回候选", () => review("reject")));
    actions.append(textLabel("审核理由", reason), row);
  } else if (version.state === "released" && id !== active.version_id) {
    const reason = node("input");
    reason.maxLength = 2000;
    actions.append(textLabel("回滚理由", reason), control("回滚到此版本", async () => {
      if (!reason.value.trim()) {
        reason.focus();
        throw new Error("请填写回滚理由。");
      }
      await api(`/versions/${id}/rollback`, "POST", {expected_release_id: active.release_id, reason: reason.value.trim()});
      announce("已回滚，新排查将使用历史规则组合。");
      await loadImprove();
    }, "quiet danger"));
  } else if (["passed", "approved"].includes(version.state) && !report) {
    actions.append(node("p", "活动版本已经变化，需要以当前版本重新回放。", "empty"));
  }
  return actions;
}

function versionCard(version, active) {
  const detail = node("details", undefined, "block version");
  const activeLabel = version.version_id === active.version_id ? " · 当前活动" : "";
  const text = `${labels[version.kind]} · ${labels[version.state]}${activeLabel}`;
  const summary = node("summary");
  summary.append(node("strong", text), node("span", `  ${shortId(version.version_id)}`, "metadata"));
  detail.dataset.version = version.version_id;
  detail.append(summary);
  detail.addEventListener("toggle", async () => {
    if (!detail.open || detail.dataset.loaded) return;
    detail.dataset.loaded = "pending";
    const status = node("p", "读取版本与回放…", "empty");
    detail.append(status);
    try {
      const record = await api(`/versions/${version.version_id}`);
      status.remove();
      detail.dataset.loaded = "true";
      detail.append(node("p", `规则 ${shortId(version.rule_version_id)} · 技能 ${shortId(version.skill_version_id)}`, "metadata"));
      if (record.changes.length) {
        const changes = node("ul");
        changes.append(...record.changes.map(change => node("li", change)));
        detail.append(changes);
      }
      if (record.review_reason) detail.append(node("p", "审核理由 " + record.review_reason, "subtle"));
      detail.append(...record.reports.map(replayTable));
      if (record.activation_events.length) {
        const events = node("details", undefined, "block");
        events.append(node("summary", "激活历史"), ...record.activation_events.map(event => node(
          "p", `${event.operation === "rollback" ? "回滚" : "发布"} · ${event.actor_id} · ${event.created_at}`, "metadata")));
        detail.append(events);
      }
      detail.append(versionControls(record, active));
    } catch (error) {
      status.textContent = error.message;
      delete detail.dataset.loaded;
    }
  });
  return detail;
}

async function loadImprove() {
  const generation = identityGeneration;
  const openVersions = [...$("versions").querySelectorAll("details.version[open]")].map(item => item.dataset.version);
  const [feedback, versions] = await Promise.all([api("/feedback"), api("/versions")]);
  if (generation !== identityGeneration) return;
  const list = $("feedback-list");
  show(list, ...(feedback.items.length ? feedback.items.map(feedbackCard) : [node("p", "当前没有运营纠正。", "empty")]));
  if (actor.role === "operator") {
    const controls = node("div", undefined, "row block");
    const start = control("运行基础层", async () => {
      const ids = [...list.querySelectorAll("input[name=feedback]:checked")].map(item => item.value);
      if (!ids.length) throw new Error("请选择至少一条已复核的纠正。");
      await submitJob({kind: "base", feedback_ids: ids});
    }, "primary");
    start.disabled = !feedback.items.some(item => item.status === "verified");
    controls.append(start, control("运行元层", () => submitJob({kind: "meta"})));
    list.append(controls);
  }
  show($("versions"), ...versions.items.map(version => {
    const card = versionCard(version, versions.active);
    if (openVersions.includes(version.version_id)) card.open = true;
    return card;
  }));
}

$("reload-improve").addEventListener("click", () => action($("reload-improve"), async () => {
  await loadImprove();
  const job = local("job");
  if (job) await watchJob(job, ++jobObservation);
}));

function evaluationCard(item) {
  const card = node("article", undefined, "block");
  card.append(node("h3", `${item.kind === "logistics" ? "物流诊断" : labels[item.kind]} · ${labels[item.mode]}`),
              node("p", `数据 ${item.data_version} · ${item.model_id || labels[item.mode]} · ${item.created_at}`, "metadata"));
  const table = node("table", undefined, "table");
  const head = table.createTHead().insertRow();
  for (const label of ["对照组", "样本量", "指标"]) head.append(node("th", label));
  const tbody = table.createTBody();
  for (const group of item.groups) {
    const row = tbody.insertRow();
    row.append(node("td", group.name + (group.name === item.baseline_group ? "（基线）" : "")),
               node("td", group.sample_count),
               node("td", Object.entries(group.metrics).map(([key, value]) => `${key} ${value ?? "—"}`).join(" · ")));
  }
  card.append(table);
  for (const sample of item.case_examples) {
    const details = node("details", undefined, "block");
    details.append(node("summary", sample.scenario), node("p", `预期 ${sample.expected}`),
      ...sample.results.map(entry => node("p", `${entry.group} · ${entry.correct ? "正确" : "错误"} · ${entry.actual}`)));
    card.append(details);
  }
  card.append(node("p", `归档 ${item.artifact_path} · 提交 ${item.code_ref}`, "metadata"));
  return card;
}

async function loadEvaluations() {
  const generation = identityGeneration;
  const query = new URLSearchParams();
  if ($("evaluation-kind").value) query.set("kind", $("evaluation-kind").value);
  if ($("evaluation-mode").value) query.set("mode", $("evaluation-mode").value);
  show($("evaluations"), node("p", "正在读取评测归档…", "empty"));
  const archive = await api("/evaluations?" + query);
  if (generation !== identityGeneration) return;
  show($("evaluations"), ...(archive.items.length ? archive.items.map(evaluationCard) :
    [node("p", "当前筛选下暂无已归档报告。", "empty")]));
}

$("evaluation-filters").addEventListener("submit", event => {
  event.preventDefault();
  void refreshView();
});

selectView();
void signIn(localStorage.getItem("logimate:profile") || "alpha_operator");
