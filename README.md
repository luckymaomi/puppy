# xhs-robot

`xhs-robot` 是 owner 自用的小红书终端机器人。它通过独立可见 Chromium 操作公开网页，使用独立 `.env` 中配置的 OpenAI-compatible Provider 根据当前可见笔记和评论生成草稿，并在验证码、登录失效、频率提示或结果不确定时立即停止，保留页面供人工接管。

默认模式会在每次发送前要求终端审核；只有显式指定 `--send-mode auto` 才会自动发送。

## 项目结构

```text
app.py                  唯一终端入口
xhs_robot/
  ai.py                 AI 提示、生成与草稿边界
  automation.py         任务循环、审核、限额和恢复
  browser.py            独立浏览器进程与 CDP 会话
  cli.py                命令解析与接线
  config.py             .env 配置读取、类型转换与校验
  evidence.py           脱敏证据与事件记录
  page.py               动态页面发现、操作与结果核验
  probe.py              单步实时探针
  tasks.py              任务状态、去重和本地持久化
tests/                  稳定业务结果测试
```

根目录的 `XhsPost（人工）.py` 是 owner 指定保留且禁止修改的人工版本，不属于机器人入口。

## 安装

要求 Windows 和 Python 3.12：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

复制配置模板并填写真实 API Key：

```powershell
Copy-Item .env.example .env
notepad .env
```

项目只从根目录 `.env` 读取 AI 配置，不读取进程环境变量，也不接受任务级模型参数。`.env` 已被 Git 忽略；可提交的 `.env.example` 包含全部字段、硅基流动默认值，以及最近一次真实 `/models` 返回的完整注释目录。

当前默认配置使用硅基流动、`https://api.siliconflow.cn/v1`、Chat Completions 和 `Pro/zai-org/GLM-5.1`。查看当前 API Key 实际可见的全部模型、执行只读健康检查，或额外执行一次真实最小生成：

```powershell
python app.py models
python app.py health
python app.py health --generate
```

`health` 会校验配置、鉴权、模型目录和默认模型可见性；`--generate` 会产生真实模型调用。所有命令只报告 `api_key_present`，不会输出密钥。

## 启动与登录

```powershell
python app.py start
```

`start` 会先执行 Provider 健康检查，再启动 Chromium；已经由本项目启动的同端口浏览器会直接复用。Chromium 会在命令退出后继续运行。请在可见浏览器中扫码或人工登录；验证码和安全校验始终由用户处理。浏览器资料、任务、AI 草稿和证据统一保存在 Git 忽略的 `.xhs-robot/`。

检查浏览器是否可连接：

```powershell
python app.py status
```

## 运行任务

默认人工审核，最多处理 10 篇笔记，每篇随机回复 1 到 2 条已有评论：

```powershell
python app.py run --keyword "AI Agent"
```

审核时可发送、编辑、跳过或暂停当前草稿。明确允许自动发送时：

```powershell
python app.py run --keyword "AI Agent" --max-notes 5 --send-mode auto
```

任务带有笔记与评论去重、每日写入硬上限、随机操作间隔和原子状态记录。常用控制命令：

```powershell
python app.py tasks
python app.py task <task-id>
python app.py pause <task-id>
python app.py resume <task-id>
python app.py cancel <task-id>
```

浏览器出现验证码、登录失效、频率提示或提交结果不确定时，任务会进入 `waiting_login` 或 `waiting_human`。结果不确定的写入不会自动重试：先检查可见页面，再明确裁决并继续。

```powershell
python app.py resolve <task-id> --result sent
python app.py resolve <task-id> --result not-sent
python app.py resume <task-id>
```

## 单步探针

自动流程之外仍可逐步读取真实页面。每次页面重绘、滚动或路由变化后都要重新 `inspect`，临时 ID 只在当前 DOM 中有效：

```powershell
python app.py snapshot --stage current-page
python app.py inspect interactive
python app.py inspect scroll
python app.py inspect links
python app.py inspect detail
python app.py act click --id node-123
python app.py act fill --id node-124 --value "AI Agent"
python app.py act press --id node-124 --value Enter
python app.py act scroll --id window --delta 700
```

`snapshot --full-page` 可能改变虚拟列表的滚动现场，只用于基线采集。

## 验证

项目完整静态验证：

```powershell
python -W error -m compileall -q app.py xhs_robot tests
python -m pytest
python app.py --help
```

2026-08-02 已真实验证硅基流动鉴权、91 个模型的动态目录、`Pro/zai-org/GLM-5.1` 可见性和 Chat Completions 最小生成。旧探针会话还真实验证过关键词搜索与虚拟列表增量滚动、图片笔记详情、单次回复、点赞恢复和遮罩关闭。当前包结构下的完整“搜索 -> 评论 -> 随机回复 -> 下一篇”链路、视频详情、验证码暂停恢复、收藏和关注尚未在新的真实登录会话中重新验证；仓库测试不能替代该外部验证。
