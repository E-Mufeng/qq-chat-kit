"""QQ 机器人入口：NoneBot2 + OneBot v11 适配器（配合 NapCat 协议端）。

连接拓扑：NapCat 作为 OneBot11 协议端，反向 WebSocket 连到本服务的
ws://127.0.0.1:8080/onebot/ws。host/port/token 一律从 .env 读取，
保持 .env 为唯一可信源（与 llm_client 一致）。
"""
import os

from dotenv import load_dotenv

load_dotenv()  # 必须在 nonebot.init() 之前，否则 access_token 读不到

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter


def _env(name: str, default):
    val = os.getenv(name)
    if val is None or val == "":
        return default
    # 端口需要是 int
    if name == "PORT":
        return int(val)
    return val


def main() -> None:
    # host/port 走标准 NoneBot 配置项（init 认）；
    # onebot_access_token 必须放在 pyproject.toml [tool.nonebot]，init 传参会被忽略。
    nonebot.init(
        driver="nonebot.drivers.fastapi",
        host=_env("HOST", "127.0.0.1"),
        port=_env("PORT", 8080),
    )
    driver = nonebot.get_driver()
    driver.register_adapter(ONEBOT_V11Adapter)
    nonebot.load_from_toml("pyproject.toml")
    nonebot.run()


if __name__ == "__main__":
    main()
