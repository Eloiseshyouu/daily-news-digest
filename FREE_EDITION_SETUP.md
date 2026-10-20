# 免费中文编辑版：候选代码，默认不发送

此版本不启用任何付费 AI 服务。原来的 `news_digest.py` 和
`daily-news.yml` 保留，旧 GitHub 工作流须继续停用，避免两套程序同时寄信。
上传代码不等于恢复邮件：仍须配置免费项目密钥、验证真实样稿，再打开发送开关。

## 与旧版不同的地方

- 从媒体直接公开的 RSS 找线索，再读取可公开访问的正文；不绕过登录、付费墙或验证码。
- 将获得的公开证据交给 Gemini 免费文本模型，选题、中文改写、合并同一事件。
- 独立第二次调用做读者视角审稿；最多修改一次并重新审稿。失败时不发送空稿或旧模板。
- 证据不足、正文读取失败、来源缺失、额度不足，均停止本次任务。
- 将审核结果绑定终稿哈希；发送前检查哈希，防止“审核一稿，寄出另一稿”。
- 跨日读取前五期已发送稿，只有实质新增信息才重复报道。
- 发信前上传当日占位记录。SMTP 结果不确定时禁止自动重试，需先核对邮箱。

## 真正保持零额外费用的前提

1. 在 Google AI Studio 使用一个**没有关联付费账单**的专用 Gemini 项目。
   仅创建一个新的 key 并不意味着所属项目是免费项目。
2. 不点击升级、不关联信用卡、不启用 Cloud Billing。只使用官方仍标为免费的文本模型。
3. 在该 GitHub 仓库的 Actions Secrets 中由本人添加 `GEMINI_API_KEY`。
   不要把密钥写进聊天、代码、日志或普通 Variables。
4. 本人确认项目确实处于免费层后，才设置 Actions Variable
   `GEMINI_FREE_TIER_CONFIRMED=1`。程序的确认开关不是对账单状态的自动检测。
5. GitHub Actions 也只使用账户已有免费分钟和存储额度；确认没有允许超额付费。
   2026-10-20已核实账户使用20/2000分钟、0/0.5GB存储，Actions额外预算为0美元且Usage blocked。
   程序不能代替GitHub账单控制；请保持此预算不变。

免费 Gemini 输入输出可能用于产品改进，只提交公开新闻和一般编辑要求。
收件邮箱、SMTP 密码、API 密钥和私人邮件不进入模型提示。
免费容量和限额可能改变，不保证每天有足够可用额度。

## 启用顺序

1. 运行 `python3 -m unittest discover -p 'test_news_*.py' -v`，不访问模型、不发邮件。
2. 上传候选文件和新工作流，但先不设置 `NEWS_MAIL_ENABLED`。
3. 配置免费项目密钥和免费层确认开关后，手动运行 **Reviewed Chinese News**，
   保持 `mode=preview`。这会调用免费 API，**不发送测试邮件**。
4. 下载 `news-preview-...` 制品，通读 `digest.html`，核查每条原文及 `digest.json` 审核结果。
   离线测试不能证明实际新闻质量已达标；必须验收一份真实样稿。
5. 手动选择`mode=test`，输入已验收预览的`preview_run_id`，发送同一份审核稿。
   不重新调用模型；样稿须在6小时内，一天最多一封测试邮件，标题明确标记测试及真实截点。
6. 测试邮件通过后设置 `NEWS_MAIL_ENABLED=true` 才会启用日常发送。旧工作流仍保持停用。
   沿用现有 SMTP、MAIL_FROM、MAIL_TO，不另外开通邮件服务。

## 时间口径

目标每日日本时间09:00发信。工作流计划08:37启动，08:53开始采集和编辑；
09:00以前不发，09:30以后不补发。正文注明实际采集时间，排除09:00后公开的信息。
正式稿必须在09:00前完成正文采集；任务启动太晚或采集超时则跳过当天发送，
避免文章在09:00后被无标记更新而混入早报。下午手动生成的预览不能直接转为正式邮件。

GitHub Actions 官方说明计划任务可能延迟甚至被丢弃，因此**不能承诺精确09:00送达**。
新闻收集、审核、网络以及邮箱投递也都需要时间。当天没通过审核不能为了准点跳过检查。

## 文件

- `news_sources.py` / `source_config.json`：公开资料采集。
- `news_editor.py`：免费 Gemini 编辑、独立审核和一次修订。
- `news_pipeline.py`：时间、预览、发信、持久化防重记录。
- `.github/workflows/news-reviewed.yml`：新工作流，默认不发送。
- `test_news_*.py`：离线安全测试。

每条固定顺序：标题、短版、详细版、来源、为什么重要。中日文正文使用语言适配的
长度过滤，仍必须通过事实证据审稿；不会仅靠标题、RSS摘要或模型记忆扩写新闻。

官方依据：

- [Gemini免费与付费功能](https://ai.google.dev/gemini-api/docs/pricing)
- [Gemini计费和项目升级](https://ai.google.dev/gemini-api/docs/billing)
- [免费服务数据条款](https://ai.google.dev/gemini-api/terms#unpaid-services)
- [GitHub计划任务延迟](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
