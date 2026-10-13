const messages = {
  401: "管理会话已过期，请重新登录",
  413: "文件或请求过大，请缩短 Fiddler 捕获时间后重新导出",
  502: "服务器暂时无法连接，请稍后刷新状态",
  503: "服务器暂时不可用，请稍后刷新状态",
  504: "服务器等待超时，请先核对状态，避免重复提交操作",
};

export class ApiError extends Error {
  constructor(message, status = 0, uncertain = false) {
    super(message);
    this.status = status;
    this.uncertain = uncertain;
  }
}

export async function requestApi(path, method = "GET", body, options = {}) {
  const write = !["GET", "HEAD"].includes(method);
  const multipart = body instanceof FormData;
  const headers = { "X-Farm-Action": "personal-control" };
  if (write) {
    const key = options.key || crypto.randomUUID();
    headers["Idempotency-Key"] = key;
    options.onKey?.(key);
  }
  if (body !== undefined && !multipart)
    headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeout ?? (write ? 140000 : 12000),
  );
  try {
    const response = await (options.fetch || fetch)("/api" + path, {
      method,
      credentials: "same-origin",
      headers,
      signal: controller.signal,
      body:
        body === undefined
          ? undefined
          : multipart
            ? body
            : JSON.stringify(body),
    });
    const text = await response.text();
    let value;
    try {
      value = text ? JSON.parse(text) : null;
    } catch {
      throw new ApiError(
        messages[response.status] ||
          `服务器返回了无法读取的响应（HTTP ${response.status}），请刷新状态`,
        response.status,
        write,
      );
    }
    if (!response.ok) {
      const message =
        typeof value?.message === "string"
          ? value.message
          : typeof value?.detail === "string"
            ? value.detail
            : messages[response.status];
      throw new ApiError(
        message || `请求未完成（HTTP ${response.status}）`,
        response.status,
        write && response.status >= 500,
      );
    }
    return value;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) {
      throw new ApiError(
        write
          ? "请求等待超时，操作可能仍在执行。请先刷新状态确认结果，避免重复提交。"
          : "读取服务器状态超时，请稍后重试",
        0,
        write,
      );
    }
    throw new ApiError(
      write
        ? "连接中断，操作结果暂未确认。请恢复连接后核对状态，避免重复提交。"
        : "暂时无法读取服务器状态，正在等待连接恢复",
      0,
      write,
    );
  } finally {
    clearTimeout(timer);
  }
}
