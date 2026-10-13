// Policy contains JSON values; Vue reactive proxies cannot be structuredClone'd.
export const copy = (value) => JSON.parse(JSON.stringify(value));

export function policyPatch(draft, base) {
  return Object.fromEntries(
    Object.entries(draft).filter(
      ([key, value]) => JSON.stringify(value) !== JSON.stringify(base[key]),
    ),
  );
}

// Keep local edits, while accepting server changes to fields the user did not edit.
export function mergePolicy(draft, base, incoming) {
  return { ...copy(incoming), ...copy(policyPatch(draft, base)) };
}

// Edits made while a save is in flight must remain pending after the response.
export function finishSave(draft, submitted, saved) {
  return { ...copy(saved), ...copy(policyPatch(draft, submitted)) };
}

export function replaceObject(target, source) {
  for (const key of Object.keys(target)) delete target[key];
  Object.assign(target, source);
}

export function sellQuantity(value, available) {
  const count = value === undefined || value === "" ? available : Number(value);
  if (!Number.isSafeInteger(count) || count <= 0 || count > available) {
    throw new Error("出售数量应为 1 到当前库存之间的整数");
  }
  return count;
}

export function validatePolicy(value) {
  const ranges = [
    ["purchase_budget", "购种预算", 0, 1000000],
    ["max_lands", "最多操作土地数", 1, 24],
    ["maturity_grace_seconds", "成熟后检查延迟", 0, 3600],
    ["reward_check_interval", "每日任务查询间隔（秒）", 60, 86400],
    ["friend_interval", "好友巡查间隔（秒）", 60, 3600],
    ["friends_per_cycle", "每类每轮最多好友数", 1, 10],
  ];
  for (const [key, label, minimum, maximum] of ranges) {
    if (
      !Number.isInteger(value[key]) ||
      value[key] < minimum ||
      value[key] > maximum
    ) {
      throw new Error(`${label}应为 ${minimum} 到 ${maximum} 之间的整数`);
    }
  }
  if (
    ![value.action_delay_min, value.action_delay_max].every(
      (v) => typeof v === "number" && Number.isFinite(v) && v >= 1 && v <= 10,
    ) ||
    value.action_delay_min > value.action_delay_max
  ) {
    throw new Error("随机等待应为 1 到 10 秒，下限不能大于上限");
  }
}
