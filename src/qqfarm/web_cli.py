import argparse
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="个人农场服务器网页控制台")
    parser.add_argument("--host", default=os.getenv("FARM_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.getenv("FARM_WEB_PORT", "8790"), type=int)
    parser.add_argument(
        "--trusted-proxies",
        default=os.getenv("FARM_TRUSTED_PROXIES", "127.0.0.1,::1"),
        help="可信反代来源 IP，逗号分隔；默认只信任本机反代",
    )
    parser.add_argument("--data-dir", default=os.getenv("FARM_WEB_DATA", "data/server"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("监听端口必须为 1～65535")
    import uvicorn

    from qqfarm.web_api import create_app

    root = Path(args.data_dir).resolve()
    app = create_app(root)
    print("管理密码保存在：" + str(root / "initial-password.txt"))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips=args.trusted_proxies,
    )


if __name__ == "__main__":
    main()
