# 阿里云 ECS 源码版本发布

这组脚本用于升级已经配置好的 DeepTutor ECS 源码部署。前端在本机构建，服务器安装 Python 依赖，检查通过后切换版本。适用于 Linux x86_64 ECS；本机构建支持 macOS，Linux 图片处理依赖会在服务器上按锁文件补齐。

## 快速使用

在仓库根目录执行：

```bash
# 检查阿里云登录、目标实例与服务器运行环境
python3 scripts/deploy_ecs.py check

# 一次完成构建、打包、上传和发布
python3 scripts/deploy_ecs.py release

# 查询当前和上一个版本、服务状态
python3 scripts/deploy_ecs.py status

# 切回上一个成功版本
python3 scripts/deploy_ecs.py rollback
```

当前机器的 `deploy/ecs.local.json` 已填好本次部署的目标。此文件被 Git 忽略。新机器先准备：

```bash
cp deploy/ecs.example.json deploy/ecs.local.json
```

将 `instance_id`、`host`、`region`、`profile` 等字段改为自己的目标。配置里只放实例信息和路径；阿里云认证使用 `aliyun` 已登录的 profile，不填 AccessKey、模型密钥或 SSH 私钥。

本机需要 Python 3.11+、Node.js 22、npm、阿里云 CLI，以及 OpenSSH 的 `sftp`、`ssh-keygen`。默认复制本机已有的 `web/node_modules` 到隔离目录构建；首次构建或依赖更新时可用：

```bash
python3 scripts/deploy_ecs.py release --install

# 国内网络可指定 npm 镜像
npm_config_registry=https://registry.npmmirror.com python3 scripts/deploy_ecs.py release --install
```

`--install` 在临时构建目录执行 `npm ci`，不会改写开发目录的 `node_modules`、`.next` 或 TypeScript 配置。pnpm 等包含目录符号链接的依赖布局需要使用此选项。

## 分阶段发布

```bash
# 1. 构建当前工作树，包含尚未提交的源码
python3 scripts/deploy_ecs.py build --release-id my-release

# 2. 打包已有构建目录，生成归档及 SHA256 校验文件
python3 scripts/deploy_ecs.py package --release dist/ecs/my-release

# 3. 本地校验并查看部署计划，不访问云端
python3 scripts/deploy_ecs.py deploy --bundle dist/ecs/my-release.tar.gz --dry-run

# 4. 发布同一个归档
python3 scripts/deploy_ecs.py deploy --bundle dist/ecs/my-release.tar.gz
```

未指定版本号时，脚本使用 UTC 时间和 Git commit 短哈希生成唯一 release ID。`--output` 可改变构建输出目录；`package --archive` 可指定归档位置；云端命令可通过 `--config` 选择其他目标。

`release --dry-run` 仍会在本地构建和打包，但不会上传或切换服务器。`check`、`status` 通过阿里云云助手读取运行环境，不修改 SSH 授权。

## 发布内容和服务器前提

产物位于 `dist/ecs/`，包含 Python 源码、原始前端源码、Next.js standalone 运行文件和 `manifest.json`。manifest 记录应用版本、Git commit、工作树是否有改动、构建平台、Linux sharp 原生包版本及逐文件 SHA256；归档旁边还有 `.tar.gz.sha256`。

打包白名单不包含 `data/`、`.env*`、云 CLI 配置、SSH 密钥、开发缓存和本机依赖目录。运行配置及用户数据继续使用 ECS 的 `/opt/deeptutor/data`，现有 Nginx、公网 HTTPS 和管理员登录设置由服务器管理。

目标 ECS 应已具备：

- 正在运行且可由指定 profile 访问的实例，云助手可用。
- 非 root 部署用户，以及允许 SFTP 的 SSH 服务和可访问的 22 端口。
- Python 3.11、Node.js 22、npm、uv；路径由目标 JSON 指定。
- 既有 `/opt/deeptutor/current` 链接、`runtime.env`、`start-backend.sh`、`data/` 与 systemd 服务。
- 至少 2 GiB 可用磁盘；新增依赖较大时应预留更多空间。

脚本不会创建 ECS、安装系统运行环境或配置域名、证书和安全组。新增需要系统库的功能，应先在服务器准备相应系统依赖。

## 上传、切换和回滚

上传使用临时 Ed25519 密钥，通过云助手授权，限制为 SFTP，20 分钟后失效。脚本从云助手获取服务器主机公钥并严格校验，上传结束立即清理授权。现有 SSH 隧道密钥保持其原有用途。

归档和远端安装脚本先复制到 root 私有目录并校验 SHA256；归档提取前会拒绝路径穿越、链接、特殊文件和重复路径。服务器版本目录为：

```text
/opt/deeptutor/
  current -> releases/<当前版本>
  previous -> releases/<上一个成功版本>
  releases/<版本>/
    .venv/
    deeptutor/
    web-runtime/
    manifest.json
    requirements-resolved.txt
  data/
  runtime.env
```

每个版本使用独立 Python 虚拟环境。安装及构建命令以部署用户运行；root 仅编排校验、链接切换和服务重启。首次脚本发布会备份启动脚本和 `runtime.env`，让启动器同时支持新版本 `.venv` 和最初部署的共享 `venv`，并给 PATH 增加当前版本的虚拟环境路径。

准备阶段失败时，当前服务继续使用原版本。切换后检查后端就绪、前端 API 代理和图片优化；检查失败会恢复原链接并重新启动。`rollback` 可在两个成功版本之间切换，也支持回到最初的源码部署。

**代码回滚不会回退数据库迁移或用户数据。** 涉及不兼容数据迁移时，升级前应另行备份 `data/` 并安排恢复方案。

旧版本和准备失败的候选目录会保留供排查；重试使用新的 release ID。清理时保留 `current` 和 `previous` 指向的目录。服务器端完整日志会由云助手返回，本地终端也会输出执行 ID；若本地进程被中断，先查询云助手执行结果和 `status`，再决定是否重试。
