import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { requestApi } from "./lib/api";
import {
  copy,
  finishSave,
  mergePolicy,
  policyPatch,
  replaceObject,
  sellQuantity,
  validatePolicy,
} from "./lib/policy";
import { duration, maturity } from "./lib/format";
import { todayLeaderboard } from "./lib/friends";

const manualPaths = new Set([
  "/sell",
  "/tasks/claim",
  "/friends/action",
  "/care",
]);
const operationNames = {
  plant: "播种",
  harvest: "收获",
  harvest_batch: "批量收获",
  cleanup: "清理土地",
  purchase: "购买种子",
  sell: "出售",
  claim_task: "领取任务",
  care: "照料农场",
  scheduler: "自动调度",
  heartbeat: "心跳",
  login: "农场登录",
  friend_help: "帮助好友",
  friend_steal: "好友采摘",
  friend_insects: "放虫",
  friend_weeds: "放草",
  care_verification: "照料结果复查",
  friend_visit_leave: "离开好友农场",
  action_delay: "操作等待",
};
const stateNames = {
  confirmed: "已确认",
  success: "成功",
  started: "执行中",
  waiting: "等待中",
  planned: "已安排",
  skipped: "已跳过",
  cancelled: "已取消",
  rejected: "接口拒绝",
  unknown: "结果未确认",
  failed: "失败",
  stopped: "已停止",
  acknowledged: "已回应",
  closed: "已断开",
  received: "已更新",
  unavailable: "暂不可用",
  reported: "已报告",
};

export function useFarm() {
  const pausing = ref(false);
  const authed = ref(false),
    password = ref(""),
    busy = ref(false),
    page = ref("overview");
  const notice = ref(""),
    error = ref(false),
    connectionError = ref(""),
    clock = ref(Date.now() / 1000);
  const data = ref(null),
    draft = reactive({}),
    basePolicy = ref({}),
    logs = ref([]);
  const search = ref(""),
    showLocked = ref(false),
    plantFilter = ref("all"),
    authFile = ref(null);
  const friendSearch = ref("");
  const sellCounts = reactive({}),
    operationId = ref(""),
    busyLabel = ref("正在处理…");
  const nav = [
    ["overview", "农场概览"],
    ["plants", "种植计划"],
    ["friends", "好友互动"],
    ["warehouse", "仓库出售"],
    ["tasks", "任务奖励"],
    ["settings", "挂机设置"],
    ["logs", "操作日志"],
  ];
  const names = {
    empty: "空地",
    growing: "生长中",
    mature: "可收获",
    residue: "待清理",
    skip: "未操作",
    unknown: "待确认",
    occupied: "关联占用",
  };
  const actions = [
    ["help", "帮助好友", "浇水、除草、除虫"],
    ["steal", "采摘", "采摘允许采摘的成熟作物"],
    ["insects", "放虫", "对生长中的无虫地块放虫"],
    ["weeds", "放草", "对生长中的无草地块放草"],
  ];
  let ticker,
    poller,
    statusRequest = null,
    serverOffset = 0,
    epoch = 0,
    pollGeneration = 0,
    disposed = false;
  const ready = computed(() =>
    ["connected", "running"].includes(data.value?.mode),
  );
  const changed = computed(
    () => Object.keys(policyPatch(draft, basePolicy.value)).length > 0,
  );
  const unlocked = computed(
    () => data.value?.seeds.filter((p) => p.unlocked).length || 0,
  );
  const visiblePlants = computed(() =>
    (data.value?.seeds || []).filter(
      (p) =>
        (showLocked.value || p.unlocked) &&
        p.name.toLowerCase().includes(search.value.trim().toLowerCase()) &&
        (plantFilter.value !== "stock" || p.inventory > 0) &&
        (plantFilter.value !== "selected" ||
          draft.seed_priority?.includes(p.seed_id)),
    ),
  );
  const ripe = computed(
    () => data.value?.lands.filter((l) => l.status === "mature").length || 0,
  );
  const fruit = computed(
    () => data.value?.inventory.filter((v) => v.sellable && v.count > 0) || [],
  );
  const leaderboard = computed(() => todayLeaderboard(data.value?.friends));
  const visibleFriends = computed(() =>
    (data.value?.friends || []).filter((f) =>
      (f.remark || f.name || "").includes(friendSearch.value.trim()),
    ),
  );
  const taskIntervalHours = computed({
    get: () => draft.reward_check_interval / 3600,
    set: (value) => {
      draft.reward_check_interval =
        value === "" ? "" : Math.round(Number(value) * 3600);
    },
  });
  const operations = computed(() => data.value?.operations || []);
  const progress = computed(
    () =>
      operations.value.find((o) => o.id === operationId.value) ||
      operations.value[0],
  );
  const num = (value) => new Intl.NumberFormat("zh-CN").format(value || 0);
  const inventoryKey = (item) => item.id + ":" + item.uid;
  const seedName = (id) =>
    data.value?.seeds.find((s) => s.seed_id === id)?.name || `种子 ${id}`;
  const friendName = (id) => {
    const f = data.value?.friends.find((x) => x.id === id);
    return f ? f.remark || f.name : "列表外好友";
  };
  const logOperation = (value) =>
    operationNames[value] ||
    (value.startsWith("rpc_") ? `游戏请求 ${value.slice(4)}` : value);
  const logState = (value) => stateNames[value] || value;
  function tell(message, failed = false) {
    notice.value = message;
    error.value = failed;
  }

  async function api(path, method = "GET", body) {
    try {
      return await requestApi(path, method, body, {
        timeout: ["/policy", "/login", "/logout", "/friends/resume"].includes(
          path,
        )
          ? 15000
          : undefined,
        onKey: (key) => {
          if (manualPaths.has(path)) operationId.value = key;
        },
      });
    } catch (e) {
      if (e.status === 401) {
        authed.value = false;
        epoch++;
      }
      throw e;
    }
  }

  async function load(options = {}) {
    if (disposed) return false;
    if (statusRequest) {
      if (!options.fresh) return statusRequest;
      const pending = statusRequest;
      await pending;
      if (statusRequest === pending) statusRequest = null;
      return load();
    }
    const requestEpoch = epoch;
    const request = (async () => {
      try {
        const status = await api("/status");
        if (disposed || requestEpoch !== epoch) return false;
        if (
          JSON.stringify(status.policy) !== JSON.stringify(basePolicy.value)
        ) {
          replaceObject(
            draft,
            mergePolicy(draft, basePolicy.value, status.policy),
          );
          basePolicy.value = copy(status.policy);
        }
        data.value = status;
        serverOffset = status.server_now - Date.now() / 1000;
        clock.value = Date.now() / 1000 + serverOffset;
        authed.value = true;
        connectionError.value = "";
        if (page.value === "logs") {
          try {
            const entries = await api("/logs");
            if (!disposed && requestEpoch === epoch) logs.value = entries;
          } catch (e) {
            tell(e.message, true);
          }
        }
        return true;
      } catch (e) {
        if (!disposed && e.status !== 401 && requestEpoch === epoch)
          connectionError.value = e.message;
        return false;
      }
    })();
    statusRequest = request;
    try {
      return await request;
    } finally {
      if (statusRequest === request) statusRequest = null;
    }
  }

  async function run(fn, message, label = "正在处理…") {
    if (busy.value) return false;
    busy.value = true;
    busyLabel.value = label;
    operationId.value = "";
    tell("");
    try {
      await fn();
      epoch++;
      await load({ fresh: true });
      if (message) tell(typeof message === "function" ? message() : message);
      return true;
    } catch (e) {
      tell(e.message, true);
      return false;
    } finally {
      busy.value = false;
      operationId.value = "";
    }
  }

  async function persistDraft() {
    const submitted = copy(draft);
    validatePolicy(submitted);
    const patch = policyPatch(submitted, basePolicy.value);
    if (!Object.keys(patch).length) return;
    const partial = data.value?.capabilities?.includes("policy_patch");
    const saved = await api(
      "/policy",
      partial ? "PATCH" : "PUT",
      partial ? patch : submitted,
    );
    epoch++;
    replaceObject(draft, finishSave(draft, submitted, saved));
    basePolicy.value = copy(saved);
    if (data.value) data.value.policy = copy(saved);
  }

  const save = () =>
    run(
      persistDraft,
      () =>
        changed.value
          ? "本次提交已保存；刚刚的新修改仍待保存"
          : "设置已保存，下个操作周期生效",
      "正在保存设置…",
    );
  function discardChanges() {
    replaceObject(draft, copy(basePolicy.value));
    tell("已撤销未保存的修改");
  }
  const start = () =>
    run(
      async () => {
        await persistDraft();
        if (changed.value)
          throw new Error("保存期间又有新修改，请保存后再开始挂机");
        await api("/start", "POST");
      },
      "挂机已启动，使用已保存的设置",
      "正在保存设置并启动挂机…",
    );
  const login = () =>
    run(
      async () => {
        await api("/login", "POST", { password: password.value });
        password.value = "";
      },
      "",
      "正在登录…",
    );

  async function pause() {
    if (pausing.value) return;
    pausing.value = true;
    try {
      await api("/pause", "POST");
      epoch++;
      await load({ fresh: true });
      tell("已暂停后续操作；已发送的请求会继续核对结果，心跳继续维护");
    } catch (e) {
      tell(e.message, true);
    } finally {
      pausing.value = false;
    }
  }

  async function logout() {
    if (busy.value) return;
    busy.value = true;
    try {
      await api("/logout", "POST");
      epoch++;
      authed.value = false;
      data.value = null;
      basePolicy.value = {};
      replaceObject(draft, {});
      replaceObject(sellCounts, {});
      logs.value = [];
      notice.value = connectionError.value = "";
    } catch (e) {
      tell(e.message, true);
    } finally {
      busy.value = false;
    }
  }

  function toggleSeed(id) {
    const list = draft.seed_priority;
    if (list.includes(id)) {
      if (
        list.length === 1 &&
        !data.value?.capabilities?.includes("empty_seed_priority")
      ) {
        tell("至少需要保留一种优先植物", true);
        return;
      }
      list.splice(list.indexOf(id), 1);
    } else if (list.length < 20) list.push(id);
    else tell("最多可设置 20 种优先植物，请先移除一种", true);
  }
  function reorder(list, index, step) {
    const next = index + step;
    if (next >= 0 && next < list.length)
      [list[index], list[next]] = [list[next], list[index]];
  }
  function toggleFriend(kind, id) {
    const ids = draft[kind + "_priority"];
    if (ids.includes(id)) ids.splice(ids.indexOf(id), 1);
    else ids.push(id);
  }
  async function importSaz(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      if (!file.name.toLowerCase().endsWith(".saz")) {
        tell("请选择 Fiddler 导出的 .saz 文件", true);
        return;
      }
      if (file.size > 40 * 1024 * 1024) {
        tell("SAZ 超过 40 MiB，请缩短捕获时间后重新导出", true);
        return;
      }
      await run(
        () => {
          const form = new FormData();
          form.append("file", file, file.name);
          return api("/authorization/saz", "POST", form);
        },
        "农场登录和心跳验证通过，可以开始挂机",
        "正在上传 SAZ 并验证农场心跳，通常需要几十秒…",
      );
    } finally {
      event.target.value = "";
    }
  }
  async function sell(item) {
    let count;
    try {
      count = sellQuantity(sellCounts[inventoryKey(item)], item.count);
    } catch (e) {
      tell(e.message, true);
      return;
    }
    if (!window.confirm(`确认出售 ${count} 个${item.name}？`)) return;
    const succeeded = await run(
      () =>
        api("/sell", "POST", {
          items: [{ id: item.id, uid: item.uid, count }],
        }),
      "出售回包已确认",
      "正在核对库存并出售…",
    );
    if (succeeded) delete sellCounts[inventoryKey(item)];
  }

  function beforeUnload(event) {
    if (changed.value) {
      event.preventDefault();
      event.returnValue = "";
    }
  }
  async function poll(generation = pollGeneration) {
    if (disposed) return;
    if (authed.value) await load();
    if (!disposed && generation === pollGeneration)
      poller = setTimeout(
        () => poll(generation),
        busy.value || operations.value.length
          ? 2000
          : document.hidden
            ? 20000
            : 5000,
      );
  }
  function visible() {
    if (!document.hidden && authed.value) {
      pollGeneration++;
      clearTimeout(poller);
      poll();
    }
  }
  onMounted(() => {
    load();
    ticker = setInterval(() => {
      clock.value = Date.now() / 1000 + serverOffset;
    }, 1000);
    poller = setTimeout(poll, 5000);
    window.addEventListener("beforeunload", beforeUnload);
    document.addEventListener("visibilitychange", visible);
  });
  onUnmounted(() => {
    disposed = true;
    epoch++;
    clearInterval(ticker);
    clearTimeout(poller);
    window.removeEventListener("beforeunload", beforeUnload);
    document.removeEventListener("visibilitychange", visible);
  });

  return {
    pausing,
    pause,
    authed,
    password,
    busy,
    page,
    notice,
    error,
    connectionError,
    clock,
    data,
    draft,
    logs,
    search,
    showLocked,
    plantFilter,
    authFile,
    friendSearch,
    sellCounts,
    nav,
    names,
    actions,
    ready,
    changed,
    unlocked,
    visiblePlants,
    visibleFriends,
    ripe,
    fruit,
    leaderboard,
    taskIntervalHours,
    operations,
    progress,
    busyLabel,
    inventoryKey,
    num,
    duration,
    maturity,
    tell,
    api,
    load,
    run,
    login,
    logout,
    save,
    start,
    discardChanges,
    toggleSeed,
    reorder,
    seedName,
    friendName,
    toggleFriend,
    importSaz,
    sell,
    logOperation,
    logState,
  };
}
