"""
百度地图 MCP 接入 — 直接复用官方地图 MCP 的 14 个工具（轮地址/POI/路线/天气等）

相比自写 WebAPI（需 AK+SK 签名，易 211）：MCP 只校验 AK，一次接入拿到全部地图能力。
接入方式：langchain_mcp_adapters 的 MultiServerMCPClient，SSE 连接 baidu MCP。
失败降级：AK 未配置或连接失败 → 返回空列表，不阻塞主链路（fail-open）。
"""
import asyncio

from config.settings import BAIDU_MAP_AK

# 百度地图官方 MCP（SSE 传输，只需服务端 AK）
_BAIDU_MCP_URL = f"https://mcp.map.baidu.com/sse?ak={BAIDU_MAP_AK}"
_MCP_SERVER_NAME = "baidu-maps"


def load_baidu_mcp_tools():
    """同步加载百度地图 MCP 工具（Agent 构建时调用一次）。

    Returns: list[BaseTool]，失败返回空列表（降级，不影响主链路）。
    """
    if not BAIDU_MAP_AK:
        print("[MapMCP] 未配置 baidu_map_ak，跳过地图 MCP 接入")
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient({
            _MCP_SERVER_NAME: {"url": _BAIDU_MCP_URL, "transport": "sse"}
        })
        tools = asyncio.run(client.get_tools())
        print(f"[MapMCP] 已加载百度地图 MCP 工具 {len(tools)} 个")
        return tools
    except Exception as e:
        print(f"[MapMCP] 加载百度地图 MCP 工具失败（降级为无地图工具）: {e}")
        return []


# 供 SYSTEM_PROMPT 追加的地图工具说明（在 master_agent 拼 prompt 时拼接）
MAP_TOOLS_DESC = """
- `map_search_places` / `map_search_pro`: 地点检索（周边/关键词/POI，竞品与商圈分析）
- `map_geocode` / `map_reverse_geocode`: 地址↔坐标互转（门店表 → 分布热力图）
- `map_directions_matrix` / `map_directions`: 距离/路线（仓库→门店配送调度）
- `map_weather` / `map_road_traffic`: 天气与实时路况
- `map_place_details`: POI 详情（评分/营业状态，便于筛选）
"""
