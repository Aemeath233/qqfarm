"""个人网页控制台：密码、同源写请求与服务端受限操作。"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from zipfile import BadZipFile

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from qqfarm import wire
from qqfarm.config import FarmError
from qqfarm.web_policy import Policy
from qqfarm.web_service import FarmService

MAX_SAZ_UPLOAD_BYTES = 40 * 1024 * 1024


class RequestBodyLimit:
    """Enforce actual bytes even when the request uses chunked transfer."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope["path"].startswith("/api/")
            or scope["method"] in {"GET", "HEAD"}
        ):
            return await self.app(scope, receive, send)
        maximum = (
            MAX_SAZ_UPLOAD_BYTES + 1024 * 1024
            if scope["path"] == "/api/authorization/saz"
            else 200_000
        )
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    raise HTTPException(413, "请求过大")
            return message

        await self.app(scope, bounded_receive, send)


class LoginInput(BaseModel):
    password: SecretStr


class AuthorizationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr
    version: str = Field(max_length=80)
    os: str = Field(default="Windows", max_length=80)
    platform: str = "qq"
    login_method: Literal["Login", "PlayerLogin"] = "Login"
    user_agent: str = Field(default="qqfarm-personal-web/1", max_length=2048)
    origin: str = Field(default="https://gate-obt.nqf.qq.com", max_length=2048)
    template: SecretStr | None = None
    friend_open_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("code")
    @classmethod
    def check_code(cls, value):
        text = value.get_secret_value()
        if (
            not text
            or len(text) > 8192
            or text.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in text)
        ):
            raise ValueError("授权无效")
        return value

    @field_validator("version", "os", "user_agent", "origin")
    @classmethod
    def no_control_characters(cls, value):
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("客户端信息无效")
        return value

    @field_validator("platform")
    @classmethod
    def only_qq(cls, value):
        if value != "qq":
            raise ValueError("仅支持QQ")
        return value

    @field_validator("friend_open_ids")
    @classmethod
    def check_friend_keys(cls, values):
        if any(not v or len(v) > 512 or any(ord(c) < 32 for c in v) for v in values):
            raise ValueError("好友同步信息无效")
        return list(dict.fromkeys(values))

    def profile(self):
        value = self.model_dump(exclude={"code", "template"})
        value["code"] = self.code.get_secret_value()
        if self.template:
            template = self.template.get_secret_value()
            if len(template) > 90000 or len(base64.b64decode(template, validate=True)) > 65536:
                raise FarmError("授权模板过大或无效")
            value["template"] = template
        return value


class SellItem(BaseModel):
    id: int = Field(gt=0)
    count: int = Field(gt=0, le=100_000_000)
    uid: int | None = Field(default=None, ge=0)


class SellInput(BaseModel):
    items: list[SellItem] = Field(min_length=1, max_length=100)


class ClaimInput(BaseModel):
    task_id: int | None = None


class FriendInput(BaseModel):
    kind: str
    friend_id: int = Field(gt=0)


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class WebAuth:
    def __init__(self, root, password=None):
        path = root / "admin.json"
        if not path.exists():
            password = password or secrets.token_urlsafe(18)
            if len(password) < 12:
                raise FarmError("管理密码至少12位。")
            salt = secrets.token_hex(16)
            path.write_text(json.dumps({"salt": salt, "hash": password_hash(password, salt)}))
            path.chmod(0o600)
            if not os.getenv("FARM_ADMIN_PASSWORD"):
                initial = root / "initial-password.txt"
                initial.write_text(password, encoding="utf-8")
                initial.chmod(0o600)
        self.password = json.loads(path.read_text())
        self.sessions = {}
        self.attempts = {}

    def login(self, password, ip):
        recent = [t for t in self.attempts.get(ip, []) if time.monotonic() - t < 60]
        self.attempts[ip] = recent
        if len(recent) >= 5:
            raise HTTPException(429, "尝试过多，请稍后再试")
        recent.append(time.monotonic())
        if len(password) > 512 or not hmac.compare_digest(
            password_hash(password, self.password["salt"]), self.password["hash"]
        ):
            raise HTTPException(401, "管理密码不正确")
        token = secrets.token_urlsafe(32)
        self.sessions = {k: v for k, v in self.sessions.items() if v > time.time()}
        self.sessions[token] = time.time() + 8 * 3600
        return token

    def check(self, request):
        token = request.cookies.get("farm_session", "")
        if self.sessions.get(token, 0) < time.time():
            raise HTTPException(401, "请登录管理面板")


def create_app(root, *, password=None, service=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    service = service or FarmService(root)
    auth = WebAuth(root, password or os.getenv("FARM_ADMIN_PASSWORD"))
    receipts = {}

    @asynccontextmanager
    async def lifecycle(_):
        yield
        await service.close()

    app = FastAPI(
        title="个人农场控制台", lifespan=lifecycle, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.service = service
    app.add_middleware(RequestBodyLimit)

    @app.middleware("http")
    async def security(request, call_next):
        if request.url.path.startswith("/api/"):
            try:
                if request.url.path in {"/api/login", "/api/authorization/saz"} and (
                    request.url.scheme != "https"
                    and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
                ):
                    raise HTTPException(403, "请通过HTTPS面板或本机SSH隧道登录和导入授权。")
                if request.method not in {"GET", "HEAD"}:
                    if request.headers.get("X-Farm-Action") != "personal-control":
                        raise HTTPException(403, "写请求校验失败")
                    origin = request.headers.get("origin")
                    expected = os.getenv("FARM_PUBLIC_URL", str(request.base_url).rstrip("/"))
                    if origin and origin.rstrip("/") != expected.rstrip("/"):
                        raise HTTPException(403, "来源校验失败")
                    max_body = (
                        MAX_SAZ_UPLOAD_BYTES + 1024 * 1024
                        if request.url.path == "/api/authorization/saz"
                        else 200_000
                    )
                    try:
                        content_length = int(request.headers.get("content-length", "0"))
                    except ValueError:
                        raise HTTPException(400, "请求长度无效") from None
                    if content_length < 0:
                        raise HTTPException(400, "请求长度无效")
                    if content_length > max_body:
                        raise HTTPException(413, "请求过大")
                if request.url.path != "/api/login":
                    auth.check(request)
            except HTTPException as exc:
                return JSONResponse({"message": exc.detail}, status_code=exc.status_code)
        response = await call_next(request)
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api/") else "no-cache"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        # FastAPI默认input回显会包含授权内容，统一省略。
        return JSONResponse({"message": "输入格式不正确，请检查设置或授权文件。"}, status_code=422)

    @app.exception_handler(FarmError)
    async def farm_error(_request, error):
        return JSONResponse({"message": str(error)}, status_code=409)

    @app.exception_handler(wire.ProtocolError)
    async def protocol_error(_request, error):
        return JSONResponse({"message": f"游戏回包格式异常，已停止操作：{error}"}, status_code=409)

    @app.post("/api/login")
    async def login(value: LoginInput, request: Request):
        token = auth.login(
            value.password.get_secret_value(), request.client.host if request.client else "local"
        )
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "farm_session",
            token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            max_age=8 * 3600,
        )
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        auth.sessions.pop(request.cookies.get("farm_session", ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("farm_session")
        return response

    @app.get("/api/status")
    async def status():
        return service.view()

    @app.get("/api/logs")
    async def logs(after: int = 0):
        if after < 0:
            raise HTTPException(400, "游标无效")
        rows = service.audit.db.execute(
            "SELECT id,at,operation,state,details FROM operations "
            "WHERE id>? ORDER BY id DESC LIMIT 100",
            (after,),
        ).fetchall()
        return [
            {"id": r[0], "at": r[1], "operation": r[2], "state": r[3], "details": json.loads(r[4])}
            for r in rows
        ]

    @app.put("/api/policy")
    async def policy(value: Policy):
        service.save_policy(value)
        return service.policy.model_dump()

    @app.patch("/api/policy")
    async def patch_policy(value: dict):
        if not set(value).issubset(Policy.model_fields):
            raise FarmError("包含不支持的设置项。")
        try:
            updated = Policy.model_validate({**service.policy.model_dump(), **value})
        except ValueError:
            raise FarmError("设置范围或格式不正确。") from None
        service.save_policy(updated)
        return service.policy.model_dump()

    @app.post("/api/friends/resume")
    async def resume_friend(value: dict):
        kind = value.get("kind")
        if kind not in {"help", "steal", "insects", "weeds"}:
            raise FarmError("好友动作无效。")
        service.friend_stats.resume(service.audit.account, kind)
        return {"ok": True}

    @app.post("/api/authorization/saz")
    async def authorize_saz(file: Annotated[UploadFile, File()]):
        if not file.filename or Path(file.filename).suffix.lower() != ".saz":
            await file.close()
            raise FarmError("请选择 Fiddler 导出的 .saz 文件。")
        try:
            archive = await file.read(MAX_SAZ_UPLOAD_BYTES + 1)
        finally:
            await file.close()
        if len(archive) > MAX_SAZ_UPLOAD_BYTES:
            raise HTTPException(413, "SAZ 文件超过 40 MiB 限制。")

        def parse_profile(payload):
            from qqfarm.capture import authorization_from_capture
            from qqfarm.crypto import GameCrypto, bundled_wasm

            crypto = GameCrypto(bundled_wasm(root / "tsdk.wasm"))
            return authorization_from_capture(payload, crypto, max_age=300)

        try:
            profile = await asyncio.to_thread(parse_profile, archive)
        except (
            BadZipFile,
            ValueError,
            EOFError,
            UnicodeError,
            OSError,
            zlib.error,
            NotImplementedError,
        ):
            raise FarmError("SAZ 格式无效或内容不完整，请重新导出。") from None
        del archive
        account = profile.pop("account_tag", "")
        profile["template"] = base64.b64encode(profile["template"]).decode("ascii")
        try:
            authorized = AuthorizationInput(
                code=profile["code"],
                version=profile["version"],
                os=profile["os"],
                platform=profile["platform"],
                login_method=profile.get("login_method", "Login"),
                user_agent=profile["user_agent"],
                origin=profile["origin"],
                template=profile["template"],
                friend_open_ids=profile["friend_open_ids"],
            ).profile()
        except ValidationError:
            raise FarmError("SAZ 中的登录信息不受支持，请重新导出最新会话。") from None
        await service.login(authorized)
        return {"ok": True, "account": account, "version": authorized["version"]}

    @app.post("/api/start")
    async def start():
        await service.start()
        return {"ok": True}

    @app.post("/api/pause")
    async def pause():
        await service.pause()
        return {"ok": True}

    @app.post("/api/disconnect")
    async def disconnect():
        await service.pause()
        async with service.guard:
            await service.disconnect()
        return {"ok": True}

    @app.post("/api/refresh")
    async def refresh():
        await service.refresh()
        return {"ok": True}

    async def mutation(request, action, **args):
        key = request.headers.get("Idempotency-Key", "")
        if not 8 <= len(key) <= 100:
            raise HTTPException(400, "操作标识缺失")
        fingerprint = hashlib.sha256(
            json.dumps([action, args], sort_keys=True).encode()
        ).hexdigest()
        if key in receipts:
            if receipts[key][0] != fingerprint:
                raise HTTPException(409, "操作标识冲突")
            raise HTTPException(409, "该操作已提交，请查看状态；不会重复发送")
        if len(receipts) > 500:
            receipts.pop(next(iter(receipts)))
        receipts[key] = (fingerprint, time.time())
        await service.manual(action, operation_id=key, **args)
        return {"ok": True}

    @app.post("/api/sell")
    async def sell(value: SellInput, request: Request):
        return await mutation(request, "sell", items=[x.model_dump() for x in value.items])

    @app.post("/api/tasks/claim")
    async def claim(value: ClaimInput, request: Request):
        return await mutation(request, "claim", task_id=value.task_id)

    @app.post("/api/friends/action")
    async def friend(value: FriendInput, request: Request):
        return await mutation(request, "friend", kind=value.kind, friend_id=value.friend_id)

    @app.post("/api/care")
    async def care(request: Request):
        return await mutation(request, "care")

    web = Path(__file__).parent / "resources/web"
    if (web / "assets").exists():
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (web / "index.html").exists():
            raise HTTPException(503, "前端尚未构建")
        return FileResponse(web / "index.html")

    return app
