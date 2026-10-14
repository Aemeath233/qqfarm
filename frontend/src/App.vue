<script setup>
import { useFarm } from "./useFarm";
const {
  authed,
  password,
  busy,
  pausing,
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
  sellCounts,
  nav,
  names,
  actions,
  ready,
  changed,
  unlocked,
  visiblePlants,
  ripe,
  fruit,
  friendSearch,
  visibleFriends,
  leaderboard,
  taskIntervalHours,
  operations,
  progress,
  busyLabel,
  inventoryKey,
  num,
  duration,
  maturity,
  api,
  load,
  run,
  login,
  logout,
  save,
  start,
  pause,
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
} = useFarm();
</script>

<template>
  <main v-if="!authed" class="login-wrap">
    <form class="login-card" @submit.prevent="login">
      <div class="brand-mark">农</div>
      <h1>农场管家</h1>
      <p class="muted">登录你的个人控制台，让农场持续生长。</p>
      <label
        >管理密码<input
          v-model="password"
          type="password"
          autocomplete="current-password"
          required
          placeholder="输入管理密码" /></label
      ><button class="primary" :disabled="busy">
        {{ busy ? "正在登录…" : "登录控制台 →" }}
      </button>
      <p v-if="notice || connectionError" class="form-error">
        {{ notice || connectionError }}
      </p>
      <button
        v-if="connectionError"
        type="button"
        :disabled="busy"
        @click="load"
      >
        重试读取状态</button
      ><small>首次密码保存在程序数据目录的 initial-password.txt</small>
    </form>
  </main>
  <div v-else-if="data" class="shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand-mark">农</span>
        <div>农场管家<small>网页挂机助手</small></div>
      </div>
      <nav>
        <button
          v-for="[key, label] in nav"
          :key="key"
          :class="{ active: page === key }"
          @click="
            page = key;
            if (key === 'logs') load();
          "
        >
          {{ label
          }}<i
            v-if="key === 'tasks' && data.tasks.some((t) => t.claimable)"
          ></i>
        </button>
      </nav>
      <div class="side-bottom">
        <div class="status-dot" :class="{ live: ready }"></div>
        {{ ready ? "农场连接正常" : "等待农场授权"
        }}<small>{{ data.running ? '关闭网页不影响运行，请保持程序运行' : ready ? '当前未挂机，等待开始' : '农场连接已断开，请重新授权' }}</small
        ><button class="text-button" @click="logout">退出面板</button>
      </div>
    </aside>
    <section class="main">
      <header>
        <div>
          <h1>{{ nav.find((n) => n[0] === page)?.[1] }}</h1>
        </div>
        <div class="toolbar">
          <input
            ref="authFile"
            type="file"
            accept=".saz,application/zip"
            hidden
            @change="importSaz"
          /><button :disabled="busy" @click="authFile.click()">
            导入 Fiddler SAZ</button
          ><button
            :disabled="busy || !ready"
            @click="run(() => api('/refresh', 'POST'), '已刷新游戏数据')"
          >
            刷新状态</button
          ><button
            v-if="!data.running"
            class="primary"
            :disabled="busy || !ready"
            @click="start"
          >
            {{ changed ? "保存并开始挂机" : "开始挂机" }}</button
          ><button
            v-if="data.running || operations.length"
            class="danger"
            :disabled="pausing || !ready"
            @click="pause"
          >
            {{ pausing ? "正在暂停…" : "暂停操作" }}
          </button>
        </div>
      </header>
      <div class="banner">
        <div>
          {{ data.note
          }}<small>{{ data.running ? '挂机进行中。关闭网页不影响运行，请保持程序运行。' : ready ? '当前未挂机，可点击开始挂机。' : '当前未挂机，请重新导入授权后开始。' }}</small>
        </div>
        <span v-if="data.heartbeat?.acknowledged" class="tag">心跳正常</span>
      </div>
      <div v-if="connectionError" class="notice error" role="alert">
        {{ connectionError
        }}<button :disabled="busy" @click="load">重试读取状态</button>
      </div>
      <div
        v-if="busy || operations.length"
        class="operation-progress"
        role="status"
        aria-live="polite"
      >
        {{ progress?.stage || busyLabel }}
        <small v-if="progress"
          >已等待
          {{ Math.max(0, Math.floor(clock - progress.started_at)) }} 秒</small
        >
      </div>
      <div v-if="notice" class="notice" :class="{ error }" role="status">
        {{ notice }}<button @click="notice = ''">×</button>
      </div>
      <div v-if="page === 'overview'" class="metrics">
        <article>
          <span>当前账号</span
          ><strong>{{ data.player.name || "待授权" }}</strong
          ><small>Lv. {{ data.player.level }}</small>
        </article>
        <article>
          <span>金币余额</span><strong>{{ num(data.player.gold) }}</strong
          ><small>本次购种花费 {{ num(data.metrics.coins_spent) }}</small>
        </article>
        <article>
          <span>我的土地</span
          ><strong>{{ data.lands.length }}<em>块</em></strong
          ><small>{{ ripe }} 块可收获</small>
        </article>
        <article>
          <span>已解锁植物</span><strong>{{ unlocked }}<em>种</em></strong
          ><small>解锁状态以游戏商店为准</small>
        </article>
      </div>

      <template v-if="page === 'overview'">
        <div class="overview-grid">
          <section class="panel">
            <div class="panel-heading">
              <div>
                <h2>我的农场</h2>
                <p>
                  按每块土地的实际成熟时间安排种收<span v-if="data.next_check">
                    · 下次检查
                    {{
                      new Date(data.next_check * 1000).toLocaleString("zh-CN", {
                        hour12: false,
                      })
                    }}</span
                  >
                </p>
              </div>
              <button
                :disabled="busy || !ready"
                @click="
                  run(
                    () => api('/care', 'POST'),
                    '照料操作已确认',
                    '正在检查土地并照料…',
                  )
                "
              >
                照料农场
              </button>
            </div>
            <div v-if="!data.lands.length" class="empty">
              导入授权后显示土地状态
            </div>
            <div class="land-grid">
              <article
                v-for="land in data.lands"
                :key="land.id"
                class="land"
                :class="land.status"
              >
                <div class="land-top">
                  <span>{{ String(land.id).padStart(2, "0") }} 号地</span
                  ><span>{{ names[land.status] }}</span>
                </div>
                <b>{{ land.name }}</b>
                <small v-if="land.status === 'growing' && land.mature_at">{{
                  maturity(land.mature_at - clock)
                }}</small>
                <small v-else-if="land.master_id"
                  >关联 {{ land.master_id }} 号主地</small
                >
                <small v-else-if="land.plot_size === 2">占用四格</small>
              </article>
            </div>
          </section>
          <section class="panel plan-panel">
            <h2>当前种植计划</h2>
            <p class="muted">从上到下优先选种；设置保存后生效。</p>
            <div
              v-for="(id, i) in draft.seed_priority"
              :key="id"
              class="priority-row"
            >
              <span class="rank">{{ i + 1 }}</span
              ><b>{{ seedName(id) }}</b>
              <button
                :disabled="busy || i === 0"
                :aria-label="seedName(id) + '上移'"
                @click="reorder(draft.seed_priority, i, -1)"
              >
                ↑
              </button>
              <button
                :disabled="busy || i === draft.seed_priority.length - 1"
                :aria-label="seedName(id) + '下移'"
                @click="reorder(draft.seed_priority, i, 1)"
              >
                ↓
              </button>
              <button
                :disabled="busy"
                :aria-label="'移除' + seedName(id)"
                title="移除"
                @click="toggleSeed(id)"
              >
                ×
              </button>
            </div>
            <p v-if="!draft.seed_priority?.length" class="muted">
              {{
                draft.fallback_radish
                  ? "未选择优先植物，将使用白萝卜回退。"
                  : "未选择植物：收获已有作物，保留空地。"
              }}
            </p>
            <hr />
            <dl>
              <dt>购种方式</dt>
              <dd>
                {{
                  draft.purchase_mode === "funds"
                    ? "按现有金币"
                    : draft.purchase_mode === "inventory"
                      ? "只用库存"
                      : num(draft.purchase_budget) + " 金币/轮"
                }}
              </dd>
              <dt>白萝卜回退</dt>
              <dd>{{ draft.fallback_radish ? "开启" : "关闭" }}</dd>
              <dt>施肥</dt>
              <dd>
                {{
                  {
                    off: "不施肥",
                    normal: "普通肥一次",
                    organic: "有机肥一次",
                  }[draft.fertilizer]
                }}
              </dd>
              <dt>自动领取每日任务</dt>
              <dd>{{ draft.auto_claim ? "开启" : "关闭" }}</dd>
              <dt>自动出售</dt>
              <dd>{{ draft.auto_sell ? "开启" : "关闭" }}</dd>
            </dl>
            <button class="primary" :disabled="busy || !changed" @click="save">
              保存当前计划</button
            ><button class="text-button" @click="page = 'plants'">
              选择植物
            </button>
          </section>
        </div>
      </template>

      <template v-if="page === 'plants'">
        <section class="panel">
          <div class="panel-heading">
            <div>
              <h2>种植计划</h2>
              <p>
                从上到下选择可种的植物；库存优先使用，购买遵循挂机设置中的购种方式。
              </p>
            </div>
            <button class="primary" :disabled="busy || !changed" @click="save">
              保存种植计划
            </button>
          </div>
          <div class="priority-strip">
            <span>种植顺序</span>
            <div
              v-for="(id, i) in draft.seed_priority"
              :key="id"
              class="priority-chip"
            >
              <span>{{ i + 1 }}. {{ seedName(id) }}</span>
              <button
                :disabled="busy || i === 0"
                :aria-label="seedName(id) + '上移'"
                title="上移"
                @click="reorder(draft.seed_priority, i, -1)"
              >
                ↑
              </button>
              <button
                :disabled="busy || i === draft.seed_priority.length - 1"
                :aria-label="seedName(id) + '下移'"
                title="下移"
                @click="reorder(draft.seed_priority, i, 1)"
              >
                ↓
              </button>
              <button
                :disabled="busy"
                class="remove-priority"
                :aria-label="'移除' + seedName(id)"
                title="从优先级移除"
                @click="toggleSeed(id)"
              >
                ×
              </button>
            </div>
            <span v-if="!draft.seed_priority?.length">{{
              draft.fallback_radish
                ? "未选择优先植物，将使用白萝卜回退。"
                : "未选择植物：收获已有作物，保留空地。"
            }}</span>
          </div>
          <div class="plant-filters">
            <div class="search-box">
              <input
                v-model="search"
                placeholder="搜索植物名称"
                aria-label="搜索植物"
              /><button
                v-if="search"
                aria-label="清空搜索"
                @click="search = ''"
              >
                ×
              </button>
            </div>
            <select v-model="plantFilter" aria-label="筛选植物">
              <option value="all">全部植物</option>
              <option value="stock">有库存</option>
              <option value="selected">已选优先植物</option>
            </select>
            <label
              ><input v-model="showLocked" type="checkbox" />显示未解锁</label
            ><span class="muted">{{ visiblePlants.length }} 种</span>
          </div>
          <div class="table-scroll">
            <table class="plant-table">
              <thead>
                <tr>
                  <th>植物</th>
                  <th>来源 / 布局</th>
                  <th>金币 / 包</th>
                  <th>种子库存</th>
                  <th>解锁条件</th>
                  <th>基础成熟时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="plant in visiblePlants"
                  :key="plant.seed_id + ':' + plant.goods_id"
                  :class="{
                    selected: draft.seed_priority.includes(plant.seed_id),
                  }"
                >
                  <td>
                    <b>{{ plant.name }}</b
                    ><small v-if="draft.seed_priority.includes(plant.seed_id)"
                      >优先级
                      {{
                        draft.seed_priority.indexOf(plant.seed_id) + 1
                      }}</small
                    >
                  </td>
                  <td>
                    {{ plant.inventory_only ? "库存种子" : "商店"
                    }}<small>{{
                      plant.layout_inferred
                        ? "布局待核实"
                        : plant.plot_size === 2
                          ? "占用四格"
                          : "单格"
                    }}</small>
                  </td>
                  <td>
                    {{
                      plant.inventory_only
                        ? "仅使用库存"
                        : plant.price > 0
                          ? num(plant.price)
                          : "未提供"
                    }}<small
                      v-if="!plant.inventory_only && plant.pack_count > 1"
                      >每包 {{ plant.pack_count }} 粒</small
                    >
                  </td>
                  <td>{{ num(plant.inventory) }}</td>
                  <td>
                    {{
                      plant.inventory_only
                        ? plant.inventory > 0
                          ? "可使用"
                          : "无库存"
                        : "Lv. " + plant.required_level
                    }}<small v-if="!plant.inventory_only">{{
                      plant.unlocked ? "已解锁" : "未解锁"
                    }}</small>
                  </td>
                  <td>
                    {{
                      plant.base_grow_seconds
                        ? duration(plant.base_grow_seconds)
                        : "未提供"
                    }}
                  </td>
                  <td>
                    <button
                      :disabled="
                        busy ||
                        (!plant.unlocked &&
                          !draft.seed_priority.includes(plant.seed_id))
                      "
                      :class="{
                        selected: draft.seed_priority.includes(plant.seed_id),
                      }"
                      @click="toggleSeed(plant.seed_id)"
                    >
                      {{
                        draft.seed_priority.includes(plant.seed_id)
                          ? "移除"
                          : !plant.unlocked
                            ? "未解锁"
                            : "加入优先级"
                      }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!visiblePlants.length" class="empty">
            {{
              data.seeds.length
                ? "没有符合筛选条件的植物"
                : "连接农场后显示可用植物"
            }}
          </p>
          <p class="footnote">
            加入优先级只修改计划，点击保存后生效，不会立即购买或播种。实际成熟时间以农场状态为准。
          </p>
        </section>
      </template>

      <template v-if="page === 'friends'">
        <section class="panel">
          <div class="panel-heading">
            <div>
              <h2>今日互动排行</h2>
              <p>
                {{ data.friend_stats?.day }} ·
                按本助手确认成功的地块互动次数累计，合计排序；只显示今天有记录的好友。
              </p>
            </div>
          </div>
          <div v-if="leaderboard.length" class="table-scroll">
            <table class="leaderboard">
              <thead>
                <tr>
                  <th>排名</th>
                  <th>好友</th>
                  <th>帮助</th>
                  <th>采摘</th>
                  <th>放虫</th>
                  <th>放草</th>
                  <th>合计</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(friend, i) in leaderboard" :key="friend.id">
                  <td>{{ i + 1 }}</td>
                  <td>
                    <b>{{ friend.remark || friend.name }}</b>
                  </td>
                  <td>{{ friend.counts.help }}</td>
                  <td>{{ friend.counts.steal }}</td>
                  <td>{{ friend.counts.insects }}</td>
                  <td>{{ friend.counts.weeds }}</td>
                  <td>
                    <b>{{ friend.total }}</b>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="compact-empty">今天尚无成功互动记录</p>
        </section>
        <section class="panel">
          <div class="panel-heading">
            <div>
              <h2>互动设置</h2>
              <p>各类操作有独立开关与好友名单，保存后生效。</p>
            </div>
            <button class="primary" :disabled="busy || !changed" @click="save">
              保存好友设置
            </button>
          </div>
          <div class="friend-strategies">
            <article v-for="[kind, label, description] in actions" :key="kind">
              <label class="toggle-line"
                ><b>{{ label }}</b
                ><input
                  v-model="draft['friend_' + kind]"
                  type="checkbox" /></label
              ><small>{{ description }}</small>
              <div v-if="data.friend_daily_limits?.[kind]" class="friend-block">
                <b>今日已达游戏上限</b>
                <p>{{ data.friend_daily_limits[kind] }}</p>
              </div>
              <div v-else-if="data.friend_blocks?.[kind]" class="friend-block">
                <b>已暂停</b>
                <p>{{ data.friend_blocks[kind] }}</p>
                <button
                  :disabled="busy || !ready"
                  @click="
                    run(
                      () => api('/friends/resume', 'POST', { kind }),
                      '已恢复此类好友操作',
                    )
                  "
                >
                  恢复{{ label }}
                </button>
              </div>
              <p v-else-if="data.friend_notes?.[kind]" class="quota">{{ data.friend_notes[kind] }}</p>
              <p v-if="data.friend_quota?.[kind]" class="quota">
                <template v-if="data.friend_quota[kind].game_limit_reached">
                  本助手今日记录 {{ data.friend_quota[kind].used }} 次；游戏已限制继续操作
                </template>
                <template v-else>
                  今日已用 {{ data.friend_quota[kind].used }} / {{ data.friend_quota[kind].maximum }}
                </template>
              </p>
              <div class="selected-friends">
                <div v-for="(id, i) in draft[kind + '_priority']" :key="id">
                  <span>{{ i + 1 }}. {{ friendName(id) }}</span
                  ><button
                    :disabled="busy || i === 0"
                    title="上移"
                    @click="reorder(draft[kind + '_priority'], i, -1)"
                  >
                    ↑</button
                  ><button
                    :disabled="
                      busy || i === draft[kind + '_priority'].length - 1
                    "
                    title="下移"
                    @click="reorder(draft[kind + '_priority'], i, 1)"
                  >
                    ↓</button
                  ><button
                    :disabled="busy"
                    :aria-label="'从' + label + '名单移除' + friendName(id)"
                    @click="toggleFriend(kind, id)"
                  >
                    ×
                  </button>
                </div>
                <p v-if="!draft[kind + '_priority'].length">
                  从下方好友列表添加
                </p>
              </div>
            </article>
          </div>
          <div class="inline-settings">
            <label
              >巡查范围<select v-model="draft.friends_scope">
                <option value="selected">仅名单内好友</option>
                <option value="all">名单优先，再检查其他好友</option>
              </select></label
            ><label
              >巡查间隔（秒）<input
                v-model.number="draft.friend_interval"
                type="number"
                min="60"
                max="3600" /></label
            ><label
              >每类每轮最多好友数<input
                v-model.number="draft.friends_per_cycle"
                type="number"
                min="1"
                max="10"
            /></label>
          </div>
        </section>
        <section class="panel">
          <div class="panel-heading">
            <h2>
              选择好友 <span class="muted">{{ data.friends.length }} 位</span>
            </h2>
            <input
              v-model="friendSearch"
              placeholder="搜索好友名称"
              aria-label="搜索好友"
            />
          </div>
          <div class="table-scroll">
            <table class="friend-table">
              <thead>
                <tr>
                  <th>好友</th>
                  <th>等级</th>
                  <th v-for="[kind, label] in actions" :key="kind">
                    {{ label }}名单
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="friend in visibleFriends" :key="friend.id">
                  <td>
                    <b>{{ friend.remark || friend.name }}</b>
                  </td>
                  <td>{{ friend.level }}</td>
                  <td v-for="[kind, label] in actions" :key="kind">
                    <button
                      :disabled="busy"
                      :class="{
                        selected: draft[kind + '_priority'].includes(friend.id),
                      }"
                      @click="toggleFriend(kind, friend.id)"
                    >
                      {{
                        draft[kind + "_priority"].includes(friend.id)
                          ? "移除"
                          : "加入"
                      }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="!visibleFriends.length" class="compact-empty">
            {{
              data.friends.length
                ? "没有符合搜索条件的好友"
                : "连接农场并同步后显示好友"
            }}
          </p>
        </section>
      </template>

      <section v-if="page === 'warehouse'" class="panel">
        <div class="panel-heading">
          <div>
            <h2>收获物仓库</h2>
            <p>
              每行对应游戏返回的一条背包记录；同名果实可能因品质或变异分开保存。参考单价不代替实际到账。
            </p>
          </div>
          <label class="toggle-line"
            >自动出售收获物<input
              aria-label="自动出售收获物"
              v-model="draft.auto_sell"
              type="checkbox"
          /></label>
        </div>
        <button v-if="changed" class="primary" :disabled="busy" @click="save">
          保存出售策略
        </button>
        <table>
          <thead>
            <tr>
              <th>收获物</th>
              <th>库存</th>
              <th>参考单价</th>
              <th>出售数量</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in fruit" :key="item.id + ':' + item.uid">
              <td>
                <b>{{ item.name }}</b>
              </td>
              <td>{{ num(item.count) }}</td>
              <td>
                {{
                  item.base_sell_price > 0
                    ? num(item.base_sell_price) + " 金币"
                    : "未提供"
                }}
              </td>
              <td>
                <input
                  v-model.number="sellCounts[inventoryKey(item)]"
                  :disabled="busy"
                  type="number"
                  min="1"
                  :max="item.count"
                  :placeholder="String(item.count)"
                />
              </td>
              <td>
                <button :disabled="busy || !ready" @click="sell(item)">
                  出售
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!fruit.length" class="empty">当前没有可出售的收获物</p>
        <h3 class="subheading">肥料余量</h3>
        <div class="fertilizer-stock">
          <article v-for="id in [1011, 1012]" :key="id">
            <span>{{ id === 1011 ? "普通化肥" : "有机化肥" }}</span
            ><b>{{
              duration(
                data.inventory
                  .filter((i) => i.id === id)
                  .reduce((sum, i) => sum + i.count, 0),
              )
            }}</b
            ><small>按游戏返回的时间余量显示，不自动购买</small>
          </article>
        </div>
      </section>

      <section v-if="page === 'tasks'" class="panel">
        <div class="panel-heading">
          <div>
            <h2>任务奖励</h2>
            <p>只领取已完成、已解锁、尚未领取的任务；不触发分享或广告翻倍。</p>
          </div>
          <label class="toggle-line"
            >自动领取每日任务<input
              aria-label="自动领取每日任务"
              v-model="draft.auto_claim"
              type="checkbox"
          /></label>
        </div>
        <button v-if="changed" class="primary" :disabled="busy" @click="save">
          保存任务策略
        </button>
        <article v-for="task in data.tasks" :key="task.id" class="task-row">
          <span class="task-icon">{{ task.type === 2 ? "✓" : "↗" }}</span>
          <div>
            <b>{{ task.description || "任务 " + task.id }}</b
            ><small
              >{{ task.type === 2 ? "每日任务" : "成长任务" }} ·
              {{ num(task.progress) }} / {{ num(task.total) }}</small
            >
            <div class="rewards">
              <span v-for="reward in task.rewards" :key="reward.id"
                >{{ reward.name }} ×{{ num(reward.count) }}</span
              >
            </div>
          </div>
          <span v-if="task.claimed" class="tag">已领取</span
          ><button
            v-else
            :disabled="busy || !ready || !task.claimable"
            @click="
              run(
                () => api('/tasks/claim', 'POST', { task_id: task.id }),
                '任务奖励已确认',
              )
            "
          >
            {{ task.claimable ? "领取奖励" : "进行中" }}
          </button>
        </article>
        <p class="footnote">
          任务完成状态来自游戏；已完成、已解锁且尚未领取时才可领奖。
        </p>
      </section>

      <template v-if="page === 'settings'">
        <div class="settings-heading">
          <p class="muted">保存后在下一操作周期生效；暂停仍维持心跳。</p>
          <button class="primary" :disabled="busy || !changed" @click="save">
            保存全部设置
          </button>
        </div>
        <div class="settings-grid">
          <section class="panel settings-panel">
            <h2>种植与自动操作</h2>
            <div class="setting-row">
              <div>
                <b>购种方式</b><small>按优先级选择，买不起时继续下一种。</small>
              </div>
              <select aria-label="购种方式" v-model="draft.purchase_mode">
                <option value="funds">按现有金币购种</option>
                <option value="inventory">只用库存</option>
                <option value="budget">限制每轮预算</option>
              </select>
            </div>
            <div v-if="draft.purchase_mode === 'budget'" class="setting-row">
              <b>每轮购种预算</b
              ><input
                aria-label="每轮购种预算"
                v-model.number="draft.purchase_budget"
                type="number"
                min="0"
                max="1000000"
              />
            </div>
            <div class="setting-row">
              <div>
                <b>白萝卜回退</b><small>优先植物都不可用时尝试白萝卜。</small>
              </div>
              <input
                aria-label="白萝卜回退"
                v-model="draft.fallback_radish"
                type="checkbox"
              />
            </div>
            <div class="setting-row">
              <b>最多操作土地数</b
              ><input
                aria-label="最多操作土地数"
                v-model.number="draft.max_lands"
                type="number"
                min="1"
                max="24"
              />
            </div>
            <div class="setting-row">
              <div><b>施肥</b><small>新播种后一次；只用已有肥料。</small></div>
              <select aria-label="施肥方式" v-model="draft.fertilizer">
                <option value="off">不施肥</option>
                <option value="normal">普通化肥一次</option>
                <option value="organic">有机化肥一次</option>
              </select>
            </div>
            <div class="setting-row">
              <b>自动浇水、除草、除虫</b
              ><input
                aria-label="自动浇水除草除虫"
                v-model="draft.auto_care"
                type="checkbox"
              />
            </div>
            <div class="setting-row">
              <b>自动领取每日任务</b
              ><input
                aria-label="自动领取每日任务"
                v-model="draft.auto_claim"
                type="checkbox"
              />
            </div>
            <div class="setting-row">
              <div><b>自动出售收获物</b><small>保留种子和肥料。</small></div>
              <input
                aria-label="自动出售收获物"
                v-model="draft.auto_sell"
                type="checkbox"
              />
            </div>
          </section>
          <section class="panel settings-panel">
            <h2>操作间隔与状态检查</h2>
            <div class="setting-row">
              <div>
                <b>操作前随机等待</b
                ><small>心跳独立运行，暂停可中断等待。</small>
              </div>
              <input
                aria-label="操作前随机等待"
                v-model="draft.random_action_delays"
                type="checkbox"
              />
            </div>
            <div class="setting-row">
              <b>等待下限（秒）</b
              ><input
                aria-label="等待下限秒"
                v-model.number="draft.action_delay_min"
                :disabled="!draft.random_action_delays"
                type="number"
                min="1"
                max="10"
                step="0.5"
              />
            </div>
            <div class="setting-row">
              <b>等待上限（秒）</b
              ><input
                aria-label="等待上限秒"
                v-model.number="draft.action_delay_max"
                :disabled="!draft.random_action_delays"
                type="number"
                min="1"
                max="10"
                step="0.5"
              />
            </div>
            <div class="setting-row">
              <div>
                <b>成熟后检查延迟（秒）</b
                ><small>默认 60 秒；每块地按实际成熟时间安排。</small>
              </div>
              <input
                aria-label="成熟后检查延迟秒"
                v-model.number="draft.maturity_grace_seconds"
                type="number"
                min="0"
                max="3600"
              />
            </div>
            <div class="setting-row">
              <b>随机播种顺序</b
              ><input
                aria-label="随机播种顺序"
                v-model="draft.random_plant_order"
                type="checkbox"
              />
            </div>
            <div class="setting-row">
              <div>
                <b>每日任务查询间隔（小时）</b
                ><small>默认 4 小时；领奖时会立即核对。</small>
              </div>
              <input
                aria-label="每日任务查询间隔小时"
                v-model.number="taskIntervalHours"
                type="number"
                :min="1 / 60"
                max="24"
                step="0.5"
              />
            </div>
          </section>
        </div>
        <div class="session-tools">
          <span>{{
            ready
              ? "农场已连接，暂停或关闭网页仍维持心跳。"
              : "需要重新授权时，点击顶部“导入 Fiddler SAZ”。"
          }}</span
          ><button
            class="danger"
            :disabled="busy || !ready"
            @click="run(() => api('/disconnect', 'POST'), '农场连接已断开')"
          >
            断开农场连接
          </button>
        </div>
      </template>

      <section v-if="page === 'logs'" class="panel">
        <div class="panel-heading">
          <div>
            <h2>操作日志</h2>
            <p>保留最近7天。操作结果未确认时停止，避免重复购买或重复领取。</p>
          </div>
          <button @click="load" :disabled="busy">刷新日志</button>
        </div>
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>操作</th>
              <th>结果</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="log in logs" :key="log.id">
              <td>
                {{
                  new Date(log.at * 1000).toLocaleString("zh-CN", {
                    hour12: false,
                  })
                }}
              </td>
              <td>{{ logOperation(log.operation) }}</td>
              <td>
                <span
                  class="tag"
                  :class="{
                    bad: ['failed', 'unknown', 'rejected'].includes(log.state),
                  }"
                  >{{ logState(log.state) }}</span
                >
              </td>
              <td class="log-details">
                <details>
                  <summary>查看详情</summary>
                  <pre>{{ JSON.stringify(log.details, null, 2) }}</pre>
                </details>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!logs.length" class="empty">暂无运行日志</p>
      </section>
      <div v-if="changed" class="save-bar">
        <span>有未保存的设置</span
        ><button :disabled="busy" @click="discardChanges">撤销修改</button
        ><button class="primary" :disabled="busy" @click="save">
          保存设置
        </button>
      </div>
      <footer>
        农场管家 {{ data.version }}
        <span
          >最新数据
          {{
            data.updated_at
              ? new Date(data.updated_at * 1000).toLocaleTimeString("zh-CN")
              : "待连接"
          }}</span
        >
      </footer>
    </section>
  </div>
</template>
