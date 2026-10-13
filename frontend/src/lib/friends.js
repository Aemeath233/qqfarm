export function todayLeaderboard(friends = []) {
  return friends
    .map((friend) => {
      const counts = Object.fromEntries(
        ["help", "steal", "insects", "weeds"].map((kind) => [
          kind,
          friend.today?.[kind]?.plots || 0,
        ]),
      );
      return {
        ...friend,
        counts,
        total: Object.values(counts).reduce((sum, count) => sum + count, 0),
      };
    })
    .filter((friend) => friend.total > 0)
    .sort(
      (a, b) =>
        b.total - a.total ||
        (a.remark || a.name || "").localeCompare(
          b.remark || b.name || "",
          "zh-CN",
        ) ||
        a.id - b.id,
    );
}
