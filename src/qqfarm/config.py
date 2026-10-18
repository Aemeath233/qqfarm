import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class FarmError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    login_code: str = field(repr=False)
    client_version: str = "1.14.2.15_20260922"
    os_tag: str = "Windows"
    platform: str = "qq"
    wasm_path: Path = Path("data/tsdk.wasm")
    seed_id: int = 20002
    purchase_budget: int = 100
    max_lands: int = 24
    request_interval: float = 1
    poll_max: float = 300
    data_dir: Path = Path("data")
    user_agent: str = field(default="qqfarm/0.1", repr=False)
    origin: str = "https://gate-obt.nqf.qq.com"
    login_template: bytes | None = field(default=None, repr=False)
    friend_open_ids: tuple[str, ...] = field(default=(), repr=False)
    random_action_delays: bool = False
    action_delay_min: float = 1
    action_delay_max: float = 3
    random_plant_order: bool = False
    maturity_grace_seconds: int = 0
    login_method: str = "Login"

    def __post_init__(self):
        if (
            not math.isfinite(self.action_delay_min)
            or not math.isfinite(self.action_delay_max)
            or not 1 <= self.action_delay_min <= self.action_delay_max <= 30
            or not 0 <= self.maturity_grace_seconds <= 3600
        ):
            raise FarmError("操作间隔应为 1～30 秒且下限不超过上限；成熟缓冲应为 0～3600 秒。")

    @classmethod
    def load(cls, *, require_login=True):
        load_dotenv(Path.cwd() / ".env", interpolate=False, override=False)
        profile_path = Path("data/session.json")
        profile = (
            json.loads(profile_path.read_text(encoding="utf-8")) if profile_path.exists() else {}
        )
        if not isinstance(profile, dict):
            raise FarmError("本地登录会话格式无效，请重新导入。")
        code = os.getenv("FARM_LOGIN_CODE", "") or profile.get("code", "")
        if not isinstance(code, str) or len(code) > 8192 or "\n" in code or "\r" in code:
            raise FarmError("登录凭证格式无效。")
        if require_login and not code:
            raise FarmError("尚未导入登录会话，请先使用 import-capture 或填写 FARM_LOGIN_CODE。")
        try:
            seed = int(os.getenv("FARM_SEED_ID", "20002"))
            budget = int(os.getenv("FARM_PURCHASE_BUDGET", "100"))
            maximum = int(os.getenv("FARM_MAX_LANDS", "24"))
            interval = float(os.getenv("FARM_REQUEST_INTERVAL", "1"))
            poll_max = float(os.getenv("FARM_POLL_MAX_SECONDS", "300"))
        except ValueError:
            raise FarmError("数字配置无效。") from None
        if not (1 <= seed < 1 << 31 and 0 <= budget <= 100000 and 1 <= maximum <= 24):
            raise FarmError("种子、购买预算或土地数量配置超出范围。")
        if not math.isfinite(interval) or not 1 <= interval <= 5:
            raise FarmError("请求间隔必须在 1～5 秒之间，避免阻塞 25 秒心跳。")
        if not math.isfinite(poll_max) or not 30 <= poll_max <= 3600:
            raise FarmError("最大检查间隔必须在 30～3600 秒之间。")

        def setting(name, key, default):
            return os.getenv(name) or profile.get(key) or default

        version = setting("FARM_CLIENT_VERSION", "version", "1.14.2.15_20260922")
        if not isinstance(version, str) or len(version) > 80:
            raise FarmError("客户端版本无效。")
        platform = setting("FARM_PLATFORM", "platform", "qq")
        if platform != "qq":
            raise FarmError("当前版本只支持本次验证的 QQ 平台。")
        os_tag = setting("FARM_OS", "os", "Windows")
        user_agent = profile.get("user_agent", "qqfarm/0.1")
        origin = profile.get("origin", "https://gate-obt.nqf.qq.com")
        for value, maximum_length in ((os_tag, 80), (user_agent, 2048), (origin, 2048)):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum_length
                or "\r" in value
                or "\n" in value
            ):
                raise FarmError("本地登录会话的客户端信息无效，请重新导入。")
        template_path = Path("data/login-template.bin")
        template = template_path.read_bytes() if template_path.exists() else None
        if template and len(template) > 65536:
            raise FarmError("登录模板过大。")
        return cls(
            code,
            version,
            os_tag,
            platform,
            Path(os.getenv("FARM_WASM_PATH", "data/tsdk.wasm")),
            seed,
            budget,
            maximum,
            interval,
            poll_max,
            user_agent=user_agent,
            origin=origin,
            login_template=template,
        )
