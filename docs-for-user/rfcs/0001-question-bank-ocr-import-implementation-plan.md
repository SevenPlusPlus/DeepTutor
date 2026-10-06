# 题库图片 / PDF 智能识别导入执行计划

- 对应 RFC：`0001-question-bank-ocr-import-mvp.md`
- 状态：Milestone 1 Complete / Milestone 2-3 In Progress
- 启动日期：2026-10-06

## 交付策略

采用可独立验收的纵向切片推进。每个 Milestone 都必须具备数据库兼容、接口级测试和明确回滚方式，不以“模型能返回一段 JSON”作为完成标准。

## Milestone 1：导入领域基础（已完成）

目标：不依赖真实视觉模型，用 fake 适配器打通识别草稿到题库的安全入库链路。

- [x] 新增 `notebook_entries.course_id` 及识别来源字段的兼容迁移
- [x] 修复结构化文件导入时 `course_id` 未持久化的问题
- [x] 统一题库列表、统计、材料、分类和练习队列的课程作用域
- [x] 把题目归一化提取为结构化导入与识别导入共享模块
- [x] 新增 `practice_recognition_jobs` 表和任务仓库
- [x] 新增 `QuestionRecognitionService` 深模块及 fake extractor
- [x] 实现答案来源与 `required` 复核门槛（缺失答案可导入，模型建议答案必须确认）
- [x] 打通 `start -> ready -> stage -> commit`
- [x] 补充迁移、课程隔离、状态机、过期和幂等测试

验收：固定识别草稿可以进入指定课程题库；缺失答案的题目可保留为待补充状态，模型建议答案无法绕过用户确认。

## Milestone 2：图片识别闭环（进行中）

目标：用户可上传一张拍照或截图，在页面中编辑识别结果并导入。

- [x] 增加 recognize / snapshot / stage HTTP 接口
- [ ] 增加文件头、大小、像素和 EXIF 方向校验
- [x] 接入支持视觉输入的任务模型
- [ ] 严格 JSON Schema 输出及一次格式修复
- [x] 保存原图并将完整原图引用关联到识别题目
- [ ] 基于题目边界生成逐题裁剪截图
- [x] 增加前端导入方式切换、进度和草稿编辑器
- [ ] 页面刷新后通过 job id 恢复
- [ ] 建立图片识别离线评测集

验收：单张 JPG、PNG 或 WebP 在正常模型响应下完成异步识别，所有题目经人工确认后才能入库。

## Milestone 3：PDF、OCR 与发布

目标：支持 20 页以内文本型和扫描型 PDF，并达到 MVP 上线门槛。

- [ ] 接入现有 `ParseService`
- [x] 增加 PDF 文件头、加密和 20 页上限校验
- [x] 将文本型和扫描型 PDF 逐页渲染为受限尺寸 PNG，通过视觉模型识别
- [x] 按页识别并持久化页码、进度与页图附件
- [ ] 合并跨页题目和独立答案页
- [ ] 增加超时、服务重启对账和过期资源清理
- [ ] 增加任务指标、模型用量和不含正文的诊断日志
- [ ] 完成异常材料、双栏、跨页和公式测试
- [ ] 达到 RFC 规定的质量和性能门槛
- [ ] 在功能开关下进行内部灰度

验收：图片和 20 页以内 PDF 均可稳定完成识别、复核和导入；关闭功能开关不影响现有结构化导入。

## 实施顺序与依赖

```text
课程归属迁移 ─┐
共享归一化器 ─┼─> 识别任务深模块 ─> 图片 API/UI ─> PDF/OCR ─> 灰度
任务表与状态机 ┘
```

课程归属修复是开放“导入到课程”的硬前置。图片识别先于 PDF，以便先验证草稿合同、答案安全和用户校验流程；PDF 只增加来源处理复杂度，不应反向影响题库入库语义。

## 验证命令

```bash
pytest -q tests/api/test_practice.py tests/api/test_notebook_router.py
pytest -q tests/services/practice/test_question_recognition_service.py
ruff check deeptutor/services/practice deeptutor/api/routers/practice.py
```

## 回滚

- Milestone 1 均为加法迁移；旧代码忽略新增列即可继续运行。
- 识别入口由 `practice.recognition_import.enabled` 控制。
- 关闭开关后禁止创建新任务，但保留已导入的普通题库条目。
- 不删除新增表和列，避免回滚造成用户数据丢失。

## 当前检查点（2026-10-06）

已可用：

- 结构化导入和图片识别导入共用归一化、stage 与 commit 语义；
- JPG、PNG、WebP 可通过配置的视觉模型生成草稿；
- 前端可切换“结构化文件 / 拍照识别”，编辑题干和答案并选择导入题目；
- 缺失答案可按 `missing` 状态 stage；模型建议答案必须改为用户确认来源后才能 stage；
- 上传原图已落入 `AttachmentStore`，预览与入库记录都保留图片引用；
- 独立导入题目可正确归属课程，并在题库及练习统计中按课程出现。

尚未完成：

- PDF 已支持 20 页内逐页视觉识别、页码和页图关联；尚未完成 `ParseService` 文本优化、跨页题目和独立答案页合并；
- 图片像素上限、EXIF 修正和题目区域裁剪；
- 逐题区域裁剪，以及识别任务过期后的附件清理生命周期；
- 页面刷新后的 job id 恢复和进程启动对账接线；
- 严格 JSON Schema、单次格式修复和真实模型离线评测。

已通过：图片 / PDF 识别服务、API 与前端工作流回归，以及 Python Ruff、TypeScript 类型检查、ESLint 和 i18n 检查。
