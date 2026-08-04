<div align="center">

# Puppy

### 小红书与哔哩哔哩公开页面匿名漫游器

打开公开页面，关闭可关闭的登录提示，逐条阅读访客可见内容，并把观察与脱敏网页证据留在本地。

[技术规格](spec.md) · [快速开始](#快速开始) · [开发规则](AGENTS.md)

<p>
  <a href="https://github.com/luckymaomi/puppy/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/luckymaomi/puppy?style=flat&amp;color=ca8a04"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-%3E%3D3.12-3776AB">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-1.58-2EAD33">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-0f766e"></a>
</p>
</div>

## Puppy 是什么

Puppy 不是账号运营机器人，也不是私有接口采集器。它使用一个全新、非持久的可见 Chromium，只做普通访客能做的公开浏览：搜索或查看推荐流、关闭可关闭的登录提示、滚动、打开内容、读取页面已经呈现的信息、关闭详情，然后继续漫游。

平台页面逻辑彼此独立：小红书适配器处理笔记、详情滚动和反复出现的登录窗；哔哩哔哩适配器处理搜索分页、视频新标签页、多层 Shadow DOM 评论以及专栏的 `cv` / `opus` 详情差异。通用总控负责任务生命周期、三种停止方式、跨页面去重、本地观察、网页证据和工作台。

## 当前能力

- 小红书公开推荐流与当前可见笔记详情
- 小红书登录弹窗的逐动作守卫与自动关闭
- 哔哩哔哩公开关键词搜索、视频分页和专栏摘要
- B 站视频标题、UP 主、简介、统计、标签与有限可见评论
- 按指定数量、指定时长或持续模式漫游
- 统一 JSON 观察，以及 PNG、脱敏 HTML、有限文本证据
- 深色本地工作台、实时事件流、暂停、继续和停止
- 独立的可选 AI Provider 配置与健康检查

Puppy 不登录、不保存长期浏览器资料、不点赞、不收藏、不关注、不评论、不回复、不发布、不下载媒体，也不逆向页面接口或处理验证码。视频画面、音频、字幕理解和 AI 观察摘要尚未接入。

## 快速开始

要求 Windows、Python 3.12 或更高版本。

```powershell
python -m pip install -e .
python -m playwright install chromium
python app.py
```

`python app.py` 会在 `127.0.0.1` 的随机端口启动工作台并打开系统浏览器。工作台中的“启动匿名浏览器”会另外打开所选平台的临时 Chromium；停止浏览器或关闭工作台时，本次浏览器资料会删除，任务、观察和脱敏证据继续留在 `.puppy/`。

匿名漫游不需要 AI 配置。需要检查自有 Provider 时，再从 `.env.example` 创建 `.env` 并填写 `PUPPY_AI_*`；API Key 不会在工作台回显。

## 工作方式

1. 在工作台选择小红书或哔哩哔哩并启动匿名浏览器。
2. 小红书可以留空关键词浏览推荐流；B 站需要填写搜索关键词。
3. 选择笔记、视频或专栏，并设置可见评论上限。
4. 选择指定数量、指定时长或持续漫游，然后启动任务。
5. Puppy 逐条执行“发现 -> 打开 -> 读取 -> 保存证据 -> 关闭 -> 继续来源”。
6. 登录窗可关闭时自动关闭；验证码、安全验证、不可关闭的阻断门禁或页面结构不确定时暂停并保留现场。
7. 在“本地观察”和“运行记录”查看它捡回的内容与真实执行事件。

持续漫游没有固定内容数、页数或滚动轮数上限。它仍会在人工停止、连续 6 轮无新增、来源耗尽、安全门禁、浏览器退出或页面失败时结束，不会空转。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `python app.py` | 打开本地工作台 |
| `python app.py browser-start --platform xiaohongshu` | 启动小红书匿名浏览器 |
| `python app.py browser-start --platform bilibili` | 启动 B 站匿名浏览器 |
| `python app.py run --platform xiaohongshu --max-items 10` | 从小红书推荐流读取 10 篇 |
| `python app.py run --platform bilibili --keyword 机器人总动员 --resource-type video --max-items 10` | 搜索并读取 10 个 B 站视频页面 |
| `python app.py observations` | 查看最近本地观察 |
| `python app.py tasks` | 查看任务历史 |
| `python app.py browser-stop` | 关闭浏览器并删除本次临时资料 |
| `python app.py health` | 检查可选 AI Provider 配置 |

使用 `python app.py --help` 查看完整命令，使用子命令的 `--help` 查看参数。

## 数据位置

| 路径 | 内容 |
| --- | --- |
| `.puppy/state/wanders/` | 匿名漫游任务 |
| `.puppy/observations/` | 按平台和资源 ID 去重的 JSON 观察 |
| `.puppy/evidence/` | 事件、视口截图、脱敏 HTML、有限文本与任务摘要 |
| `.puppy/browser/` | 当前临时浏览器资料，关闭后删除 |

`.env`、`.puppy/` 和 `.puppy-probe/` 均被 Git 忽略。不要提交任何 Cookie、会话资料、API Key 或真实页面证据。

## 项目边界

Puppy 与 `kitty` 是两个独立项目。本仓库目前不依赖 `kitty`，也尚未实现跨项目消息合同。未来可让 `kitty` 消费 Puppy 已落盘的统一观察对象或调用明确的本地接口，但平台适配器仍只负责页面事实。

真实页面已验证事实、当前限制和验收标准见 [spec.md](spec.md)。

## License

[MIT](LICENSE)
