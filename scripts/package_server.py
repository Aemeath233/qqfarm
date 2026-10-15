"""只打包源码、静态页面与部署资料；不收录运行数据或本机授权。"""

import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from qqfarm import __version__

SERVER_MODULES = {
    "__init__.py",
    "audit.py",
    "capture.py",
    "catalog.py",
    "client.py",
    "config.py",
    "crypto.py",
    "engine.py",
    "friend_stats.py",
    "heartbeat.py",
    "models.py",
    "web_api.py",
    "web_authorization.py",
    "web_cli.py",
    "web_policy.py",
    "web_service.py",
    "wire.py",
}
LEGACY_RESOURCE_FILES = {"remote_icon.ico"}
PERSONAL_DEPLOYMENT_FILES = {"rank-deployment.md"}


def main():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / name
        for name in (
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "THIRD_PARTY.md",
            "licenses/tsdk-wasm-MIT.txt",
            ".env.example",
            "docs/网页部署说明.md",
            "docs/server-helper.md",
            "docs/code-review-20261013-v061.md",
            "docs/code-review-20261015-v066.md",
            "docs/changes.md",
        )
    ]
    for name in ("src", "deploy"):
        for folder, directories, names in os.walk(root / name):
            directories[:] = [
                d
                for d in directories
                if d
                not in {
                    "node_modules",
                    "__pycache__",
                    ".pytest_cache",
                    ".ruff_cache",
                }
            ]
            if Path(folder).resolve() == (root / "deploy").resolve():
                names[:] = [n for n in names if n not in PERSONAL_DEPLOYMENT_FILES]
            if Path(folder).resolve() == (root / "src/qqfarm").resolve():
                names[:] = [n for n in names if n in SERVER_MODULES]
            files.extend(
                Path(folder) / n
                for n in names
                if not n.endswith((".pyc", ".log"))
                and not n.startswith(".env")
                and n not in LEGACY_RESOURCE_FILES
            )
    if not (root / "src/qqfarm/resources/web/index.html").is_file():
        raise RuntimeError("请先构建前端页面")
    output = root / "dist" / f"qqfarm-web-{__version__}-server.zip"
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, "qqfarm/" + path.relative_to(root).as_posix())
    print(str(output))
    print(
        f"已打包 {len(files)} 个文件，{output.stat().st_size} 字节；未包含 data、授权或管理密码。"
    )


if __name__ == "__main__":
    main()
