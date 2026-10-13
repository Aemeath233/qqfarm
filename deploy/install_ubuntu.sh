#!/usr/bin/env bash
set -euo pipefail

if [[ "$EUID" -ne 0 ]]; then
    echo "请使用 sudo bash deploy/install_ubuntu.sh。" >&2
    exit 1
fi

QQFARM_PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "$QQFARM_PROJECT_DIR" != /opt/qqfarm ]]; then
    echo "请将部署包解压到 /opt/qqfarm 后执行本脚本。" >&2
    exit 1
fi

QQFARM_UV_BIN="$(command -v uv || true)"
if [[ -z "$QQFARM_UV_BIN" && -x /root/.local/bin/uv ]]; then
    QQFARM_UV_BIN=/root/.local/bin/uv
fi
if [[ -z "$QQFARM_UV_BIN" ]]; then
    echo "请先安装 uv，再执行本脚本。" >&2
    exit 1
fi

"$QQFARM_UV_BIN" sync --project "$QQFARM_PROJECT_DIR" --locked --no-dev --python /usr/bin/python3
if ! id qqfarm >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/qqfarm --shell /usr/sbin/nologin qqfarm
fi
install -d -m 700 -o qqfarm -g qqfarm /var/lib/qqfarm
install -d -m 700 /etc/qqfarm
if [[ ! -e /etc/qqfarm/qqfarm.env ]]; then
    install -m 600 "$QQFARM_PROJECT_DIR/deploy/qqfarm.env.example" /etc/qqfarm/qqfarm.env
fi
install -m 644 "$QQFARM_PROJECT_DIR/deploy/qqfarm-web.service" /etc/systemd/system/qqfarm-web.service
systemctl daemon-reload
systemctl enable qqfarm-web
systemctl restart qqfarm-web
systemctl --no-pager status qqfarm-web
echo "后端默认监听 127.0.0.1:8790，管理密码位置：/var/lib/qqfarm/initial-password.txt"
echo "请编辑 /etc/qqfarm/qqfarm.env 填写 FARM_PUBLIC_URL，再配置你的 HTTPS 反代。"
