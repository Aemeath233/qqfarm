import test from "node:test";
import assert from "node:assert/strict";
import { reactive } from "vue";
import {
  copy,
  finishSave,
  mergePolicy,
  policyPatch,
  sellQuantity,
} from "../src/lib/policy.js";
import { duration, maturity } from "../src/lib/format.js";
import { todayLeaderboard } from "../src/lib/friends.js";

test("server updates accept unedited fields while preserving the local priority", () => {
  const base = { seed_priority: [1], auto_sell: false, friend_interval: 300 };
  const draft = { ...base, seed_priority: [1, 2] };
  const incoming = { ...base, auto_sell: true, friend_interval: 600 };
  assert.deepEqual(mergePolicy(draft, base, incoming), {
    ...incoming,
    seed_priority: [1, 2],
  });
  assert.deepEqual(policyPatch(draft, base), { seed_priority: [1, 2] });
});

test("editing during a save remains unsaved after the response", () => {
  const submitted = { seed_priority: [1, 2], auto_sell: false };
  const current = { seed_priority: [2], auto_sell: true };
  const result = finishSave(current, submitted, submitted);
  assert.deepEqual(result, current);
  assert.deepEqual(policyPatch(result, submitted), current);
});

test("reactive Vue drafts can be copied without mutating their arrays", () => {
  const draft = reactive({ seed_priority: [1, 2] });
  const result = copy(draft);
  result.seed_priority.pop();
  assert.deepEqual([...draft.seed_priority], [1, 2]);
});

test("zero or invalid sale quantities never fall back to selling all stock", () => {
  for (const value of [0, "0", -1, NaN, 1.5, 11])
    assert.throws(() => sellQuantity(value, 10));
  assert.equal(sellQuantity("", 10), 10);
  assert.equal(sellQuantity(undefined, 10), 10);
  assert.equal(sellQuantity(2, 10), 2);
});

test("zero fertilizer is shown as zero duration; only a maturity countdown says matured", () => {
  assert.equal(duration(0), "0秒");
  assert.equal(maturity(0), "已成熟");
  assert.equal(duration(undefined), "未提供");
});

test("daily leaderboard excludes inactive friends and sums affected plots instead of batch calls", () => {
  const rows = todayLeaderboard([
    { id: 1, name: "没有互动" },
    {
      id: 2,
      name: "好友乙",
      today: { help: { plots: 5, calls: 1 }, steal: { plots: 2, calls: 1 } },
    },
    {
      id: 3,
      name: "好友甲",
      today: { insects: { plots: 4 }, weeds: { plots: 4 } },
    },
  ]);
  assert.deepEqual(
    rows.map((row) => row.id),
    [3, 2],
  );
  assert.equal(rows[0].total, 8);
  assert.deepEqual(rows[1].counts, { help: 5, steal: 2, insects: 0, weeds: 0 });
});
