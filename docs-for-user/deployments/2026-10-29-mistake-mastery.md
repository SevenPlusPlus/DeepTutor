# 阿里云 ECS 发布记录：错题掌握闭环

- 发布日期：2026-10-29（Asia/Shanghai）
- 结果：发布成功，前后端健康检查通过
- 发布标识：`20261029T155236Z-mastery-93d7865`
- 应用版本：`1.6.12`（本轮使用独立发布标识区分代码）
- 来源：Git `93d78659dafce6854b350fd63728ec05ea456ba1` 的当前工作树，包含已验证的未提交源码；manifest 记录 `dirty=true`
- 目标：杭州 ECS `i-bp164t5zih1gkdjeh221`，`116.62.195.246`
- 入口：[今日复习](https://116.62.195.246:8443/learning/practice)、[微信收题](https://116.62.195.246:8443/learning/practice?view=photos)

## 发布内容

本轮将已实现的错题推荐、标签检索、独立作答与答案查看记录、掌握证据及自动关闭/重开、变式生成审核、微信拍照入库衔接一并发布。使用服务器原有运行配置、账号、资料空间和模型配置，未复制本地验收数据库。

发布包在本机隔离构建，包含当前工作树源码、Next.js standalone 运行文件及逐文件校验清单，排除用户数据和密钥；服务器创建本版本独立 Python 虚拟环境，并安装锁定版本的 Linux 图片处理依赖。

- 本地归档：`dist/ecs/20261029T155236Z-mastery-93d7865.tar.gz`
- SHA256：`72a13fd89bbf79aadd84890afdd26d00932fc15a1ab0e901e4a3cc52d3be5d48`
- 当前链接：`/opt/deeptutor/current` → `/opt/deeptutor/releases/20261029T155236Z-mastery-93d7865`
- 上一版本：`/opt/deeptutor/previous` → `/opt/deeptutor/releases/20261029`

## 数据备份与迁移

切换前在服务器私有目录 `/opt/deeptutor/backups/before-20261029T155236Z-mastery-93d7865` 备份数据和启动配置。12 个 SQLite 数据库使用在线 backup API 生成一致快照，逐库 `quick_check` 通过；快照与完整性清单只保存在服务器，没有下载用户数据。

发布后两个题库数据库均已具备本轮掌握字段、证据表和变式表，没有待执行迁移。题目总数升级前后均为 2，数据库完整性检查通过。其他运行配置继续沿用服务器已有值，认证保持启用。

## 验收

- 部署工具测试 30 项通过，隔离生产构建与发布包校验通过。
- `deeptutor-backend.service`、`deeptutor-frontend.service` 均为 `active/running`，重启计数均为 0。
- 后端 `/health/ready` 返回 200，状态为 `ready`。
- 前端 `/api/auth/status` 返回 200，`enabled=true`；公网同一路径返回 200，HTTPS 证书校验通过。
- 未登录访问复习页与照片题库入口返回 307 跳转登录；推荐及微信收件箱 API 返回 401，未放宽权限。
- 部署器检查前端 API 代理及图片优化成功；Python 依赖一致性检查及 Linux sharp 加载检查通过。
- 真实浏览器沿用已有登录会话，成功打开线上新版今日复习页并加载真实状态统计；微信收件箱成功加载并显示空状态，全程未创建题目或复习记录。

发布成功不代表已经完成真实微信发图或模型生成质量专项验收。本轮未发送测试微信消息、未调用收费模型生成，也未把样例掌握历史写入线上数据。

## 回滚

在保有本机目标配置与云账号权限的仓库目录执行：

```bash
python3 scripts/deploy_ecs.py status
python3 scripts/deploy_ecs.py rollback
```

脚本会切回上一成功版本并重新检查服务。代码回滚不恢复数据库；本轮迁移为增量变更，升级前的数据快照另外保留。不要在服务运行时直接覆盖数据库文件。
