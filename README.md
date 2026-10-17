# QQ 聊天机器人（qq-chat-kit）

一套**可移植的 QQ 私聊代聊 + 自动扩列**机器人工程。拉下本目录、填好自己的 `.env` 和本人档案，
配一个 NapCat 协议端 + 登录的 QQ，就能跑。

> **Python 版本说明**：本工程在 **Python 3.10+** 本机环境**实测通过**，但 **3.10+ 不是硬性门槛**——
> 项目只用到 Python 标准库与常见第三方库，理论上 3.8+ 亦可运行，只是我们未在更低版本上做全量验证。
> 若你用更低版本遇到语法/依赖问题，通常只需按第 10 节调整对应依赖版本，无需改业务代码。

> 衍生自开源项目 [goutoujunshi](https://github.com/powerycy/goutoujunshi)，做了大量二次开发
> （RAG 语料层、阶段化说辞、去 AI 味硬拦截、可配置本人档案、跨平台启动脚本等）。
> 见 `NOTICE` 了解来源与协议义务。

---

## 1. 它是什么

一句话：**它是一个"以你的口吻，替你聊 QQ 私聊、还能自动扩列开场"的本地机器人**。
它不是客服、不是群管、不是图灵测试玩具，而是把你本人的说话习惯、关系推进节奏，以及一份聊天心理学
知识库，封装进一个跑在本机的 QQ 客户端里。

整体由四部分拼成：

- **大脑（NoneBot2 + OneBot v11 适配器）**：跑一个本地 HTTP/WS 服务（默认 `127.0.0.1:8080`），
  不对外网暴露。它负责接收消息、调度生成、落盘记忆、执行管理命令。
- **协议端（[NapCat](https://github.com/NapNeko/NapCatQQ)，QQ NT 的 OneBot 实现）**：以**反向 WebSocket**
  连回上面的服务，真正负责收发 QQ 消息、自动通过好友请求。你登录的 QQ 号是真实账号，只是收发动作由它代劳。
- **生成层（任意 OpenAI 兼容大模型）**：云端（如 GPT / DeepSeek / GLM）或本地 ollama 都行，
  产出"你本人"口吻的回复。内部做了三道保险：
  1. **阶段策略（stage_policy）**：按"初识 → 破冰 → 升温 → 暧昧 → 确认 → 冷却 → 淘汰"的关系阶段，
     控制每句话的分寸与推进力度，不会一上来就表白或查户口。
  2. **去 AI 味硬拦截（llm_client 的 `_ai_tells`）**：命中明显 AI/客服/情感导师腔，
     或"被质疑是机器人时"的送人头破绽（如主动提出"你出题我验证"），会被强制重写或拒发。
  3. **RAG / 知识库参考（只当素材，不照抄）**：从 `knowledge/` 的 50 篇聊天心理学文档 +
     可选的本地语料索引里取素材，给 LLM 当背景，而不是复制粘贴。
- **记忆（按 QQ 号隔离的本地 SQLite）**：每个对象一条独立档案 + 滚动对话窗口，存本地 `data/memory.db`，
  不联网、不上传、不进 git。

### 核心特性

- **像你，不像机器人**：以你的 `user_profile.json`（名字 / 城市 / 兴趣 / 口头禅）为口吻基底，
  配合去 AI 味拦截，尽量抹掉"AI 味"。
- **分阶段推进，不冒进**：关系阶段可自动演进，也可号主手动修正（`/stage`），避免冷场或越界。
- **自动扩列**：`QQ_AUTO_KUOLIE=true` 时，自动通过好友请求并按对方昵称 / 附言生成**专属**开场白
  （绝不群发同一条，规避腾讯"同消息多收件人"检测）。
- **白名单 + 命令隔离**：默认只代聊白名单内的号；管理命令仅号主可用，陌生人触发无任何回执。
- **完全本地、可移植**：记忆 / 日志全在本地，clone 到任意机器填好配置即可运行，无中心化服务。

## 2. 目录结构

```
qq-chat-kit/
├── bot.py                  # NoneBot 入口（反向 WS 服务端）
├── llm_client.py           # LLM 编排：记忆+RAG+两层 prompt+去AI味硬拦截
├── goutou_prompt.py        # 系统 prompt 内核（含"被质疑是机器人"硬规则）
├── stage_policy.py         # 关系阶段策略（初识→破冰→升温→暧昧→确认→冷却→淘汰）
├── memory_store.py         # 按 QQ 号隔离的本地 SQLite 记忆
├── rag_retriever.py        # 本地语料 FTS5 检索（可选，关掉也能跑）
├── knowledge_loader.py     # knowledge/ 策略文档路由（零依赖）
├── plugins/
│   └── chat_reply.py       # 消息处理 + 自动扩列 + 远程管理命令
├── knowledge/              # 50 篇聊天/心理学策略文档（RAG-lite 用，无隐私）
├── corpus/                 # 语料索引构建脚本（可选；13.4GB 语料本身不入库）
│   ├── fetch_corpus.py
│   ├── build_index.py
│   ├── convert_psy.py
│   └── fetch_books.py
├── docs/                   # 上游/本工程补充文档
├── scripts/
│   ├── start.bat           # Windows 一键：建 venv + 装依赖 + 启动
│   └── start.sh            # Linux / macOS 一键启动
├── data/                   # 运行时数据（记忆/白名单/日志），已被 .gitignore 排除
├── .env.example            # 配置模板
├── user_profile.example.json  # 本人档案模板
├── requirements.txt
├── pyproject.toml
├── .gitignore
└── NOTICE                  # 来源与协议声明
```

## 3. 前置要求

1. **Python 3.8+ 即可运行**（安装时勾选 "Add to PATH"）。3.10+ 是本工程**实测基线**，不代表更低版本一定失败；
   用 3.8/3.9 如遇依赖不兼容，按第 10 节调整即可，不必卡在 3.10。
2. **一个已登录的 QQ**（建议小号，避免主号风控）。
3. **NapCat** 协议端：
   - Windows 桌面端：下载 NapCat 安装包，或用 [NapCatQQ Shell 版](https://github.com/NapNeko/NapCatQQ/releases)。
   - 任意平台也可跑 NapCat 容器 / Linux 版。
   - 一份可用的 QQ NT 客户端（NapCat 需要）。

## 4. 快速开始（5 步）

### 第 1 步：准备配置

```bash
# 复制模板
cp .env.example .env
cp user_profile.example.json user_profile.json   # 真实档案，勿入库
```

- 编辑 `.env`：至少填 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` 三项。
- 编辑 `user_profile.json`：填你自己的名字、城市、兴趣、说话习惯等（决定口吻）。

### 第 2 步：启动大脑（本工程）

```bash
# Windows
scripts\start.bat
# 或 Linux / macOS
bash scripts/start.sh
```

首次会自动建 `.venv` 并装依赖。看到 `Running NoneBot...` 即成功（此时还没连 QQ）。

### 第 3 步：配置 NapCat（反向 WS）

在 NapCat 的 OneBot11 配置里，添加**反向 WebSocket 客户端**：

```
url: ws://127.0.0.1:8080/onebot/v11/ws
token: （留空，与 .env 的 ONE_BOT_ACCESS_TOKEN 保持一致即可）
```

启动 NapCat 并登录你的 QQ。控制台出现 `Bot <你的QQ> connected` 即链路打通。

> 详细图文见 `docs/NapCat接入与运行指南.md`。

### 第 4 步：加白名单

机器人默认**只对白名单内的 QQ 号代聊**。两种方式：
- 在 `.env` 写 `QQ_WHITELIST=1234567890,1111111111`；
- 或号主（SUPERUSERS 里的号）发 `/allow 1234567890` 动态加。

### 第 5 步：开聊

用白名单里的号给机器人发消息，即可收到"你本人"口吻的自动回复。
号主可随时发 `/test 在吗` 看拟回（不发、不污染记忆），或 `/review` 看最近处理记录。

## 5. 配置项速查（.env）

| 项 | 作用 | 默认 |
|---|---|---|
| `LLM_BASE_URL` | OpenAI 兼容接口地址 | `https://api.openai.com/v1` |
| `LLM_API_KEY` | 接口密钥 | 必填 |
| `LLM_MODEL` | 模型名 | `gpt-4o-mini` |
| `GT_STYLE` | 默认风格：稳健/会撩/直接 | `会撩` |
| `GT_JSON` | 强制 JSON 输出（弱模型改 false） | `true` |
| `GT_RAG` | 是否启用本地语料检索 | `true` |
| `GT_RAG_K` / `GT_RAG_CHARS` | 检索条数 / 字数上限 | `6` / `2000` |
| `GT_CORPUS_ROOT` | 语料库目录（放 corpus.db）；不填则跳过 RAG 检索 | （空=跳过） |
| `QQ_AUTO_REPLY` | 自动回复白名单私聊 | `true` |
| `QQ_AUTO_KUOLIE` | 自动通过好友请求并开场 | `true` |
| `QQ_REPLY_DELAY` | 回复随机延迟上限（秒，降风控） | `3` |
| `QQ_ALLOW_ALL` | 危险：对所有私聊代聊 | `false` |
| `QQ_WHITELIST` | 初始白名单（逗号或 JSON 数组） | 空 |
| `HOST` / `PORT` | NoneBot 监听地址 | `127.0.0.1` / `8080` |
| `SUPERUSERS` | 管理员 QQ 号（远程控制） | 必填 |

## 6. 远程管理命令（仅 SUPERUSERS 生效）

| 命令 | 作用 |
|---|---|
| `/test <话>` | 跑完整生成管线，回执拟回（不发、不污染记忆） |
| `/review` | 查看最近 25 条处理记录 |
| `/allow <QQ>` / `/deny <QQ>` | 加/移出代聊白名单 |
| `/wl` | 列出当前白名单 |
| `/stage [阶段]` | 查看/手动修正关系阶段 |
| `/reset` | 清除与你的记忆（防串号） |
| `/who` | 列出当前所有对象与阶段 |
| `/style 稳健\|会撩\|直接` | 临时切换风格 |

> 注意：所有 `/` 命令**只对 SUPERUSERS 响应**；陌生人的命令一律静默忽略，不会泄露任何信息。

## 7. 安全边界（默认已开）

- **白名单**：默认只代聊白名单内的号；`QQ_ALLOW_ALL=true` 才会对所有人生效（高风险，默认关）。
- **命令隔离**：管理命令仅号主可用，陌生人触发无任何回执。
- **差异化开场**：自动扩列时按对方昵称/附言生成专属开场白，绝不群发同一条（规避腾讯"同消息多收件人"检测）。
- **本地优先**：记忆与日志全在本地 `data/`，不联网、不上传；`data/` 已被 `.gitignore` 排除。
- **去 AI 味**：命中明显 AI/客服/情感导师腔、或"被质疑是机器人时"的送人头破绽，会被底层硬拦截并重写或拒发。

## 8. 可选：本地语料 RAG

想要更贴真实聊天质感的"辅导层"素材，可按 `corpus/` 里的脚本自建索引：

```bash
# 1) 下载/整理语料（脚本会拉公开语料源）
python corpus/fetch_corpus.py
# 2) 建 FTS5 索引，产出 corpus.db
python corpus/build_index.py
# 3) 把 GT_CORPUS_ROOT 指向该目录（含 corpus.db）
```

没有 `corpus.db` 时，RAG 自动跳过，机器人只靠 `knowledge/` 策略文档 + LLM 自身能力，**不影响运行**。

## 9. 常见问题

- **`Bot connected` 一直不出现**：检查 NapCat 反向 WS 的 url 是否为 `ws://127.0.0.1:8080/onebot/v11/ws`，且本工程已先启动在 8080。
- **回复是空 / 被拒发**：看号主收到的 `[拒发]` 回执或 `data/handler.log`。常见是 LLM 返回非 JSON，或命中去 AI 味拦截——调低 `GT_RAG`、或换支持 `json_object` 的模型。
- **想换模型**：改 `.env` 的 `LLM_MODEL` + `LLM_BASE_URL` + `LLM_API_KEY`，重启即可。模型决定"上限多高"，代码决定"下限不崩"。
- **隐私**：`data/`、`user_profile.json`、`.env` 都不入库。换电脑时**不要**拷贝 `data/memory.db`（含他人聊天记录）。

## 10. 跨 Python 版本与依赖说明（开源注意事项）

本工程**不假定**所有用户都用同一套 Python 与依赖版本。为了让更多环境开箱即用，做以下约定：

- **`requires-python` 只是建议下限**：`pyproject.toml` 声明 `>=3.8`，3.10+ 是**我们实测通过**的版本，
  不代表更低版本不可用。若你在 3.8/3.9 上跑，遇报错先按下方处理，不必强行升级 Python。
- **`requirements.txt` 是本机实测快照，不是封闭/穷举清单**：它来自本地 Python 3.10+ 环境的依赖组合，
  记录"能跑的版本搭配"，**不意味着其他版本不能用，也不含所有平台变体**。
- **不要写死 `==x.y.z` 死版本**：当前清单只写包名，交给 pip 自动解析兼容版本。换 Python 版本或平台时，
  若某包报 `requires Python >= X`，按报错把该包降级到兼容版（如 `pip install 包名==兼容版本`）即可，业务代码无需改动。
- **推荐安装顺序**：先 `pip install -U pip`，再 `pip install -r requirements.txt`；
  一次性失败就逐包安装，更易定位哪个包与你的 Python 版本冲突。
- **必装核心依赖**：`nonebot2` / `nonebot-adapter-onebot` / `openai` / `python-dotenv` / `fastapi` / `uvicorn` / `websockets`。
  其中 `fastapi`+`uvicorn`+`websockets` 是 NoneBot 默认 driver 与 OneBot 反向 WS 的硬依赖，缺一则 `bot.py` 启动即 `ImportError`。
- **可选依赖（仅 RAG 语料用）**：`jieba`（中文分词）、`wikiextractor`（维基 dump 解析）。不装也能跑，只是 `corpus/` 脚本不可用；
  RAG 检索本身在无 `corpus.db` 时自动跳过。

## 11. 本工程的方法与局限（第一版说明）

当前采用的部署路径：

> **手动 Python 工程 + 独立配置的 NapCat 协议端**——拉下本仓库，自己 `pip` 装依赖，
> 再按 `docs/NapCat接入与运行指南.md` 把 NapCat 反向连到本工程。

这是我们**本机验证可行**的"第一版"做法：链路透明、可调试，但步骤偏多，属于相对"笨"的办法。

### 更优的替代路径（推荐优先尝试）

如果上游 [goutoujunshi](https://github.com/powerycy/goutoujunshi) 提供**一体化安装器**（把 NapCat 与机器人本体打包成
一条命令 / 一个桌面程序，免去手动配反向 WS），那通常是更省事、更不易出错的方案。
本"手动版"正是为**无法使用一体化安装器、或想自己掌控每一步**的场景兜底保留的。

> 说明：当前这一版我们**尚未把一体化安装方案实现/收录进来**（受本机环境与时间所限），
> 但它应作为后续版本的优先方向提出，并在开源时一并告知用户"还有更简单的安装方式"。

无论走哪条路，机器人核心（代码、prompt、去 AI 味拦截、阶段策略、记忆）都是同一套，区别只在"怎么把 NapCat 接上来"。

## 12. 致谢 / Acknowledgements

本工程是站在多个开源项目肩膀上的**个人自用 demo**，在此一并致谢：

- **[goutoujunshi](https://github.com/powerycy/goutoujunshi)**（MIT License）：本项目的思路与代码 DNA 来源。
  我们大量借鉴了它的代聊 / 关系顾问设定，并在其基础上做了二次开发（RAG 辅导层、阶段化说辞、去 AI 味硬拦截、
  NapCat/OneBot 对接、可配置本人档案、跨平台启动脚本等）。感谢原作者把项目开源，让这种玩法能被普通人复刻与改造。
- **[NapCatQQ](https://github.com/NapNeko/NapCatQQ)**：提供了稳定可用的 QQ NT OneBot 协议端，
  是"机器人能真正收发 QQ 消息"的基石。
- **[NoneBot2](https://github.com/nonebot-dev/nonebot2)** 与 **nonebot-adapter-onebot**：本工程的事件调度与
  OneBot v11 接入框架。

> 说明：本工程骨架与思路主要复用上述项目，原创部分集中在 RAG 辅导层、阶段化关系推进策略、去 AI 味硬拦截、
> 可配置人设与跨平台一键脚本。它更像一个"把现成积木按自己需求拼起来的自用玩具"，而非从零造轮子。
> 感谢这些开源作者，让这一切成为可能。
