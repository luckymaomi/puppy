<div align="center">

# 🐾 Puppy

### 赛博小狗

**不登录，不介入，只是走走看看。**

[技术规格](spec.md) · [快速开始](#快速开始) · [开发规则](AGENTS.md)

<p>
  <a href="https://github.com/luckymaomi/puppy/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/luckymaomi/puppy?style=flat&amp;color=ca8a04"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-%3E%3D3.12-3776AB">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-1.58-2EAD33">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688">
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-0f766e"></a>
</p>

</div>

---

## 它是什么？

Puppy 是一只电子小狗，替你到小红书和 B 站的公开街区里散步。

它不登录。每次出门都使用新的临时浏览器资料，不携带账号、Cookie 或上次的会话。

它不介入。不点赞、不评论、不收藏、不关注、不发布——只路过，不在页面上留下互动。

它漫步，而非互动。关键词、数量和时长只决定去哪里、什么时候回家，不会变成点赞或评论任务。它打开页面，像普通访客一样浏览，逐条把当前公开的信息记下来，整理好带回家给你。

---

## 散步方式

出门前，你可以告诉 Puppy 想去哪里：

- **小红书**：留空关键词，它会沿着推荐流随性漫步；也可以填写关键词，在当前匿名页面提供可用搜索框时搜索。
- **哔哩哔哩**：给它一个关键词，它会翻翻视频和专栏，把看到的东西告诉你。

你可以设定散步时长、想看多少条内容，也可以让它一直逛到你喊停为止。

路上遇到登录弹窗，Puppy 会顺手关掉（能关的话）。遇到验证码或不可逾越的门禁，它会乖乖停下来等你指示，不会硬闯。

---

## Puppy 会带回来什么？

每看到一条内容，Puppy 会把沿途的见闻整理成四样东西：

1. **观察笔记**（JSON）：结构化的页面信息——标题、作者、简介、统计数字、可见评论……
2. **现场照片**（截图）：它当时看到了什么画面。
3. **页面标本**：保留页面结构，并清理脚本、表单值和 URL 查询参数。
4. **文本摘录**（TXT）：保存有限的当前可见文字，便于快速查看。

这些东西都存放在本地的 `.puppy/` 文件夹里，你可以随时翻看。

---

## 快速带它出门

**需要**：Windows、Python 3.12+

```powershell
python -m pip install -e .
python -m playwright install chromium
python app.py
```

运行最后一条命令，会打开一个本地工作台。在工作台里点击“启动匿名浏览器”，Puppy 就出发了。

> Puppy 散步不需要 AI 配置，开箱即用。`.env.example` 中的 AI Provider 配置目前只用于模型目录和连通性检查，尚未接入观察总结。

---

## 命令行遛狗

```powershell
# 启动浏览器（小红书）
python app.py browser-start --platform xiaohongshu

# 小红书上随便逛 10 篇笔记
python app.py run --platform xiaohongshu --max-items 10

# B 站搜索“机器人总动员”，看 10 个视频
python app.py run --platform bilibili --keyword 机器人总动员 --resource-type video --max-items 10

# 翻翻 Puppy 之前带回来的观察记录
python app.py observations

# 查看散步历史
python app.py tasks

# 召回 Puppy，关闭浏览器
python app.py browser-stop
```

---

## Puppy 的家

所有 Puppy 带回来的东西，都放在这里：

| 路径 | 放什么 |
|------|--------|
| `.puppy/state/wanders/` | 每次散步的任务记录 |
| `.puppy/observations/` | 结构化的页面观察 |
| `.puppy/evidence/` | 截图、脱敏 HTML、文本摘要 |
| `.puppy/browser/` | 临时浏览器资料（启动时新建，停止浏览器或关闭工作台时删除） |

> `.env`、`.puppy/` 和 `.puppy-probe/` 都不会被 Git 跟踪。不要提交任何真实页面数据或密钥。

---

## Puppy 的信条

- **不登录**：不给平台任何身份信息，每次都是新面孔。
- **不介入**：只读公开内容，不执行平台写操作，不逆向接口，也不绕过门禁。
- **漫步而非互动**：关键词和停止条件只限定浏览范围，不产生点赞、评论或发布目标。

---

## 与 Kitty 的关系

Puppy 和 [Kitty](https://github.com/luckymaomi/kitty) 是两只看各自风景的独立宠物。目前它们各走各的路，未来 Puppy 收集的观察笔记，可能会成为 Kitty 的食粮——但那也是以后的事了。

真实页面已验证事实、当前限制及验收标准详见 [spec.md](spec.md)。

---

## License

[MIT](LICENSE)
