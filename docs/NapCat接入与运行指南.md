# 狗头军师 · NapCat 接入与运行指南

> 适用：NoneBot2 + OneBot v11 适配器（本项目 `bot.py`），协议端用 NapCat。
> 已验证：服务端已在本机 `127.0.0.1:8080` 运行，模拟 NapCat 反向 WS 连接成功，
> 私聊消息能进入 `plugins/chat_reply.py` 并被白名单/审核逻辑正确处理。

---

## 一、当前状态

- NoneBot 服务：**已在运行**，监听 `127.0.0.1:8080`，等待 NapCat 反向 WS 连入。
- 连接路径（千万别写错）：`ws://127.0.0.1:8080/onebot/v11/ws`
  ⚠️ 是 `/onebot/v11/ws`，**不是** `/onebot/ws`（后者会被 403 拒绝）。
- OneBot 层 token：**未设**（留空）。因为服务只绑 `127.0.0.1`（本机回环，外网不可达），
  鉴权由应用层 `SUPERUSERS` + 代聊白名单负责。NapCat 端 access_token 栏**留空即可**。
- 当前为**审核模式**（`QQ_AUTO_REPLY=false`）：对方发来的消息会在控制台
  打印「[对方 xxx] 消息 / [代聊] 回复 / [记忆]」，但**不实际发送**，方便你先看代聊质量。

---

## 二、NapCat 端操作（你本机）

1. 安装并打开 NapCat（基于 QQ NT 的 OneBot 协议端，GitHub: `NapNeko/NapCatQQ`）。
2. 登录 **QQ 你的QQ号（小号）**（建议用小号，避免主号风控）。
3. 进入 NapCat 配置 → OneBot11 → 添加 / 编辑一个「**反向 WebSocket**」：
   - **URL**：`ws://127.0.0.1:8080/onebot/v11/ws`
   - **access_token**：留空
   - 其余默认（NapCat 会自动带 `X-Self-ID` 等头，本服务需要这个头才能握手）。
4. 启用该连接，扫码/登录后，NapCat 会主动连到上面的地址。

---

## 三、连接成功标志

本服务控制台（运行 `bot.py` 的那个窗口/后台任务）会出现：
```
[INFO] OneBot V11 | Bot 你的QQ号（小号） connected
```
此时对方给你发私聊，控制台会打印 `[对方 xxx] ... / [代聊] ...`，但暂不发送。

> 若控制台**没有** `Bot ... connected`，而 NapCat 侧报连接失败：
> - 检查 URL 路径是否为 `/onebot/v11/ws`（最常见坑）；
> - 确认 NoneBot 服务还在跑（后台任务可能被回收，需让我重启）；
> - 确认 NapCat 与 NoneBot 在**同一台机器**的 `127.0.0.1` 互通（本机沙盒与本机共享 localhost，正常可通）。

---

## 四、从「审核」切到「自动代聊」

确认代聊质量 OK 后，编辑 `.env`：
```
QQ_AUTO_REPLY=true      # 真正自动回复
QQ_AUTO_KUOLIE=true     # 自动通过好友请求并自动发开场白（物色对象时开）
```
改完**重启** NoneBot 服务生效。

---

## 五、管理命令（仅管理员号 你的QQ号（小号） 可用，陌生人发命令会被静默忽略）

| 命令 | 作用 |
|---|---|
| `/who` | 查看当前所有对象及其阶段/话题 |
| `/allow <QQ号>` | 把某 QQ 加入代聊白名单 |
| `/deny <QQ号>` | 停止代聊该 QQ |
| `/wl` | 查看代聊白名单 |
| `/stage` | 查看当前阶段；`/stage 升温` 等可手动修正 |
| `/reset` | 清除与该对象的记忆（防串号） |
| `/style 稳健|会撩|直接` | 切换全局话术风格 |

> 默认 `QQ_ALLOW_ALL=false`：只有白名单内的号才会被代聊。
> 物色对象时，可先 `/allow` 目标号，或开 `QQ_AUTO_KUOLIE` 让通过的好友自动入白名单。

---

## 六、已知约定（来自对接排查，避免再踩）

- NoneBot2.5 的 OneBot 适配器只从 `[tool.nonebot]` 读 `onebot_access_token`，
  且只校验 HTTP 头 `Authorization: Bearer`，不读 URL query；故本项目直接留空，
  依赖本机回环 + 应用层鉴权。
- 反向 WS 路径在源码里硬编码为 `/onebot/v11/ws`（见 `adapter.py` 的 `WebSocketServerSetup`）。
- 连接必须带 `X-Self-ID` 头（NapCat 自动带），缺失会被 `close(1008)` 拒绝。
