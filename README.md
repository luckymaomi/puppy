<div align="center">

# XHS Robot

### 维持账号活跃度的小红书机器人

设定关键词和数量，自动完成每天的互动任务——搜索、点开、阅读、评论、回复、关闭、循环。

[技术规格](spec.md) · [快速开始](#快速开始) · [开发规则](AGENTS.md)

<p>
  <a href="https://github.com/luckymaomi/xhs-robot/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/luckymaomi/xhs-robot?style=flat&amp;color=ca8a04"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-%3E%3D3.12-3776AB">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-1.58-2EAD33">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-0f766e"></a>
</p>
</div>

## 解决的需求

小红书账号的权重依赖于日常互动行为。新账号需要积累活跃度，老账号需要维持互动频率，目标话题下需要持续出现。

平台对账号质量的判断依据之一，是账号是否产生正常的互动行为。在目标话题下评论和回复，向平台表明这是一个活跃的真实账号。账号权重通过这种行为累积，推荐流量也依赖这种维持。

人工完成这些动作的路径是：打开小红书、搜索关键词、逐篇点开笔记、阅读正文和评论区、撰写评论、回复一两条已有评论、关闭详情、进入下一篇，重复数十次。每一步本身不复杂，但重复性极高，时间消耗显著，且需要持续执行。

XHS Robot 自动化完成这个流程。设定关键词和目标数量后，系统自动执行搜索、浏览、评论和回复，完成每日互动任务。账号活跃度得以维持，目标话题下的曝光得以保持，重复性操作由系统接管。

## 运行方式

系统启动一个真实 Chrome 浏览器实例，登录小红书账号后，通过页面控件完成浏览、点击和输入操作。所有交互发生在真实页面上，操作过程可见。

执行流程：

1. 输入关键词，执行小红书搜索
2. 从搜索结果逐篇打开笔记详情
3. 读取笔记正文和评论区上下文
4. 调用 AI 生成相关评论并提交
5. 随机选择 1 到 2 条评论，分别生成回复并提交
6. 关闭详情，返回搜索结果列表，进入下一篇
7. 循环执行，直到达到设定的目标数量

验证码、登录失效或安全校验出现时，系统自动暂停并发出通知。人工处理完成后点击继续，任务恢复执行。

## 永久免费

XHS Robot 本体免费使用。只需准备自己的 API Key（OpenAI 或兼容接口），或接入本地模型。无按量计费，无云平台绑定，无限次使用，无限时运行。所有数据存储在本机。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `python app.py` | 打开本地工作台 |
| `python app.py status` | 查看浏览器状态 |
| `python app.py health` | 检查 AI 配置 |
| `python app.py tasks` | 查看任务历史 |
| `python app.py snapshot` | 保存当前页面证据 |
| `python app.py inspect` | 探测当前页面结构 |

## 本地工作台

`python app.py` 在浏览器中打开本地工作台。工作台可以随时启动或停止专用 Chromium；停止浏览器或正常关闭工作台时，运行中的任务会保存为暂停状态，本地登录资料、任务和证据仍会保留。功能以模块化方式组织：

- **AI 配置**：设置 Provider、API Key 和模型，支持 OpenAI 兼容接口
- **任务控制**：设定关键词、目标数量、回复数和发送模式，创建并启动任务
- **浏览器控制**：启动、检查登录或完整停止专用浏览器
- **执行日志**：直接展示搜索、打开笔记、读取上下文、点击发送、回复和关闭详情等真实执行事件
- **证据回溯**：查看运行过程中的截图与页面快照

## 两种运行模式

| 模式 | 行为 |
|:---:|---|
| 手动批准 | AI 生成后在工作台展示预览，确认后才发送 |
| 全自动 | 设定后连续执行，全程无需干预 |

## License

[MIT](LICENSE)
