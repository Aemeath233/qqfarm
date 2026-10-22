# QQ 农场管家

一个单账号的 QQ 农场挂机工具，使用浏览器操作。可以在自己的电脑运行，也可以部署在服务器上；不需要额外安装桌面客户端。

后端使用 Python / FastAPI，前端使用 Vue 3。当前版本：**0.6.12**。

**自动种菜、收获和好友互动需要程序持续运行，并保持联网。** 网页只是控制面板，关掉网页不会停止挂机；关闭程序、关闭启动它的终端、关机或休眠都会中断。本机运行适合体验；如果你有服务器，推荐部署到服务器，用后台服务保持运行，就不用一直开着自己的电脑。

## 快速使用

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Git，然后执行：

```bash
git clone https://github.com/Aemeath233/qqfarm.git
cd qqfarm
uv sync --locked --no-dev
uv run --locked --no-dev qqfarm-web
```

uv 会按项目要求准备 Python 3.12 和依赖。仓库已包含构建好的网页，直接运行不需要安装 Node.js。

1. 打开 **<http://127.0.0.1:8790>**。
2. 从 `data/server/initial-password.txt` 读取首次生成的管理密码，登录网页。
3. 按下面的步骤，用 Fiddler 抓取自己的农场登录并保存为 `.saz`。
4. 在网页点击 **导入 Fiddler SAZ**。登录记录需在 **5 分钟内**，文件不超过 **40 MiB**。
5. 设置种植优先级和挂机选项，保存后点击 **开始挂机**。

关闭浏览器后，程序会继续运行；关闭运行程序的终端或让电脑休眠，会中断挂机。程序重启或农场授权失效后，需要重新抓包、上传并开始挂机。

## SAZ 是什么，怎么获取？

**SAZ（Session Archive Zip）是 Fiddler 保存网络请求和回复的抓包文件。** 本项目从中提取农场临时授权和登录信息，连接你的农场；不需要手动复制 Cookie。以下使用 Windows 上的 Fiddler Classic：

1. 从 [官网下载 Fiddler Classic](https://www.telerik.com/fiddler/fiddler-classic)，安装后打开。
2. 进入 **Tools → Options → HTTPS**，勾选 **Decrypt HTTPS traffic**，按软件提示安装、信任其解密证书。可参考 [官方 HTTPS 设置说明](https://www.telerik.com/fiddler/fiddler-classic/documentation/configure-fiddler/decrypthttps)。
3. 先关闭 QQ 农场小程序，清空 Fiddler 的旧会话；确认 **File → Capture Traffic** 已开启。
4. 登录电脑 QQ，重新打开 QQ 农场小程序，等待成功进入农场。Fiddler 中应出现 `gate-obt.nqf.qq.com` 的农场连接。
5. 关闭农场小程序，让这次连接结束，然后使用 **File → Save → All Sessions** 保存为 `.saz`。这是 [Fiddler 官方的 SAZ 保存方式](https://www.telerik.com/fiddler/fiddler-classic/documentation/save-and-load-traffic/createsaz)，不要导出成 HAR 或只保存截图。
6. 在 **5 分钟内**把 SAZ 上传到农场管家。这里的时间从游戏成功登录算起，刚导出旧记录也不能使用。

授权成功后可以退出 Fiddler，挂机期间不需要它一直运行。SAZ 可能包含其他应用的请求和临时凭证，请只上传到自己的程序，不要提交到 GitHub。

## 功能

- **农场概览**：账号、金币、土地状态和成熟倒计时。
- **种植计划**：按优先级种植，支持活动库存种子；金币不足时尝试下一种，也可开启白萝卜回退。
- **好友互动**：分别设置帮助、采摘、放虫、放草，查看今日互动排行和次数。
- **仓库出售**：查看收获物，按实际库存记录出售。
- **任务奖励**：查看每日任务并领取已完成奖励。
- **挂机设置与日志**：施肥、照料、出售、随机等待和运行记录。

程序按游戏返回的成熟时间安排检查，默认成熟后延迟 **60 秒**；每日任务默认 **4 小时**检查一次。邮件附件由用户在游戏内领取。

好友默认每 **6 分钟**巡查一次；发现即将成熟的作物，会按预计成熟时间追加一次检查。采摘和帮助随机等待后会重新核对好友土地。作物不需要照料、果实已被摘走时自动跳过；游戏提示达到当天操作上限时当天停手，次日自动恢复。未知错误仍保留暂停和提示。

## 服务器运行（推荐，可选）

服务器上也可以使用上面的命令。长期挂机建议按 [部署说明](docs/网页部署说明.md) 安装为后台服务；仅在 SSH 终端里启动，退出终端后可能会停止。默认监听 `127.0.0.1:8790`，只允许本机访问。

需要远程访问时，可以使用 SSH 隧道：

```bash
ssh -N -L 8790:127.0.0.1:8790 用户名@服务器地址
```

随后在自己电脑打开 <http://127.0.0.1:8790>。**反向代理不是必需的**；如果希望通过公网域名访问，再自行配置 HTTPS 反代。

Ubuntu 的后台服务及反代配置见 [部署说明](docs/网页部署说明.md)。

## 源码结构

```text
src/qqfarm/        Python 后端、农场协议与调度
frontend/src/      Vue 网页和界面逻辑
tests/            后端测试
frontend/tests/   前端测试
deploy/           Ubuntu 服务与可选反代配置
scripts/          服务器打包脚本
docs/             使用、部署与审查记录
licenses/         第三方许可证
```

运行数据保存在 `data/server/`，不提交到 Git。SAZ 含有临时授权信息，请保管好；程序不保存上传的原始 SAZ，运行会话会加密保存。

### 修改前端

安装 Node.js/npm 后执行：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

构建结果写入 `src/qqfarm/resources/web/`。开发时可运行 `npm --prefix frontend run dev`；另一个终端启动后端，并将其环境变量 `FARM_PUBLIC_URL` 设置为 `http://127.0.0.1:5173`。

### 打包

```bash
uv run --locked --no-dev python scripts/package_server.py
```

输出 `dist/qqfarm-web-0.6.12-server.zip`，不含运行数据、密码或抓包文件。

## 许可证

本项目原创代码与文档按 [MIT License](LICENSE) 开源。内置第三方资源保留原版权与许可声明，详见 [THIRD_PARTY.md](THIRD_PARTY.md)。
