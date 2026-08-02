# xhs-robot

`xhs-robot` 当前是一个由 agent 逐步操作的小红书实时页面探针。浏览器作为独立可见进程运行；每一步先保存当前视口和脱敏页面证据，再根据真实页面结构决定下一次操作。

旧 `XhsPost.py` 会直接执行关注、点赞和评论，不是当前入口。`XhsPost（人工）.py` 是禁止修改的保留版本。

## 环境

- Windows
- Python 3.12
- Playwright 1.58

安装：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 启动

```powershell
python app.py start
```

命令完成后 Chromium 仍会保持打开。登录、扫码、验证码和安全校验全部由用户在可见浏览器中处理，不需要复制 Cookie。

本地浏览器资料和探针证据保存在 `.xhs-probe/`，该目录已被 Git 忽略。关闭浏览器窗口即可结束会话。

## 实时探测

读取当前页面状态：

```powershell
python app.py status
```

保存当前视口截图、脱敏 HTML 和有限文本摘要：

```powershell
python app.py snapshot --stage current-page
```

整页截图可能触发页面滚动，只用于不需要保留滚动位置的基线：

```powershell
python app.py snapshot --stage baseline --full-page
```

读取当前页面中的可交互元素、滚动容器、链接、详情媒体或 frame：

```powershell
python app.py inspect interactive
python app.py inspect scroll
python app.py inspect links
python app.py inspect detail
python app.py inspect frames
```

`inspect` 会为本轮可见元素分配临时 `probe_id`。agent 只能使用刚观察到的临时 ID 执行一个动作，然后重新截图和检查页面：

```powershell
python app.py act click --id node-123
python app.py act fill --id node-124 --value "AI Agent"
python app.py act press --id node-124 --value Enter
python app.py act scroll --id window --delta 700
python app.py act back
```

页面重绘后临时 ID 可能失效，应重新运行 `inspect`，不能把本次 ID 或页面 class 固化为长期选择器。

当前实测中，搜索结果由窗口滚动并使用虚拟列表；应跨轮累计唯一笔记 ID。整页截图可能重置滚动位置，不能用于实时滚动阶段。详情可通过命中遮罩空白区后点击关闭，但每次都要重新确认遮罩和关闭结果，不能固定坐标。

## 写入边界

- 默认只读探测。
- 点赞、收藏、评论和回复基于当前登录账号的交互权限执行，系统不限制目标笔记的归属。
- 提交前必须确认目标、内容和控件状态；提交后必须用页面文本、计数或状态变化核验。
- 结果不确定时不得自动重试。
- 验证码、安全校验或登录失效出现时立即停止操作，等待人工处理。

## 验证

```powershell
python -m py_compile app.py evidence.py
python app.py --help
```

真实页面能力以当次 `.xhs-probe/evidence/<run-id>/` 中的证据为准，不能用静态检查替代。

2026-08-02 已真实验证图片笔记详情、取消并恢复点赞、回复一条评论以及点击遮罩关闭详情；视频详情、整篇笔记评论提交、验证码暂停恢复、收藏和关注仍未验证。
