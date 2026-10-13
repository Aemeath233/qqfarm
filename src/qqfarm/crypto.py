"""运行固定校验值的协议 WASM；无 WASI、文件系统或网络导入。"""

import threading
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

import wasmtime

WASM_SHA256 = "748ca5e807a41e26ee81e21d8f71e231c0832c1252bf3f4814ba0cd92d5aedff"
WASM_REVISION = "5fc7f0f089137ec4140aab3e72778cd5e8f4b6ef"
WASM_URL = (
    "https://raw.githubusercontent.com/chuankkk/qq-farm-bot/"
    + WASM_REVISION
    + "/ws-keeper/utils/tsdk.wasm"
)


def bundled_wasm(destination: Path):
    """将安装包内的固定协议模块写入当前用户的数据目录，并返回校验后的路径。"""
    destination = Path(destination)
    if not destination.exists():
        asset = files("qqfarm").joinpath("resources/tsdk.wasm")
        try:
            binary = asset.read_bytes()
        except FileNotFoundError:
            raise CryptoError("安装包缺少农场协议模块。") from None
        if sha256(binary).hexdigest() != WASM_SHA256:
            raise CryptoError("安装包内农场协议模块校验失败。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(binary)
        temporary.replace(destination)
    if sha256(destination.read_bytes()).hexdigest() != WASM_SHA256:
        raise CryptoError("本机农场协议模块校验失败。")
    return destination


class CryptoError(RuntimeError):
    pass


class GameCrypto:
    def __init__(self, path: Path):
        data = path.read_bytes()
        if sha256(data).hexdigest() != WASM_SHA256:
            raise CryptoError("WASM 校验不通过，请重新运行 setup-crypto。")
        config = wasmtime.Config()
        config.consume_fuel = True
        self.engine = wasmtime.Engine(config)
        module = wasmtime.Module(self.engine, data)
        self.store = wasmtime.Store(self.engine)
        self.store.set_limits(memory_size=128 * 1024 * 1024, instances=1, memories=1, tables=4)
        self.store.set_fuel(50_000_000)
        linker = wasmtime.Linker(self.engine)
        for imp in module.imports:
            if (
                imp.module != "a"
                or imp.name not in "abcdefghijklmnopqrstu"
                or not isinstance(imp.type, wasmtime.FuncType)
            ):
                raise CryptoError("WASM 包含未允许的导入。")
            result_count = len(imp.type.results)
            if result_count > 1:
                raise CryptoError("不支持的 WASM 导入返回类型。")
            result = 0.0 if result_count and str(imp.type.results[0]) == "f64" else 0
            linker.define_func(
                imp.module,
                imp.name,
                imp.type,
                lambda *args, value=result, count=result_count: value if count else None,
            )
        self.exports = linker.instantiate(self.store, module).exports(self.store)
        self.memory = self.exports["v"]
        self.exports["w"](self.store)
        self.lock = threading.Lock()

    def transform(self, data: bytes, *, decrypt=False) -> bytes:
        if not data:
            return b""
        if len(data) > 1024 * 1024:
            raise CryptoError("业务消息过大。")
        with self.lock:
            self.store.set_fuel(50_000_000)
            pointer = self.exports["z"](self.store, len(data))
            if not pointer:
                raise CryptoError("WASM 无法分配业务缓冲区。")
            try:
                self.memory.write(self.store, data, pointer)
                self.exports["K" if decrypt else "J"](self.store, pointer, len(data))
                return bytes(self.memory.read(self.store, pointer, pointer + len(data)))
            except (wasmtime.Trap, wasmtime.WasmtimeError) as exc:
                raise CryptoError("业务消息加解密失败。") from exc
            finally:
                self.exports["A"](self.store, pointer)

    def encrypt(self, data: bytes):
        return self.transform(data)

    def decrypt(self, data: bytes):
        return self.transform(data, decrypt=True)
