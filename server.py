import os
import datetime
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from mem0 import Memory
from dotenv import load_dotenv
from starlette.types import ASGIApp, Scope, Receive, Send

# --- 1. 初始化配置 ---
load_dotenv()

# 获取钥匙
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")

# --- 2. 组装云端大脑 (Mem0 + Qdrant + OpenAI) ---
# 这是你想要的 Mem0 核心！
config_cloud = {
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "xiaoju_memory",
            "url": os.environ.get("QDRANT_URL"),
            "api_key": os.environ.get("QDRANT_API_KEY"),
        }
    },
    "llm": {
        "provider": "openai",
        "config": {
            "model": "gpt-4o-mini",
            "temperature": 0.1,
            "max_tokens": 2000,
            "openai_base_url": os.environ.get("OPENAI_BASE_URL"), 
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": "text-embedding-3-small",
            "openai_base_url": os.environ.get("OPENAI_BASE_URL"),
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
    }
}

print(f"🧠 正在连接 Mem0 云端大脑...")
m = Memory.from_config(config_cloud)
notion = Client(auth=notion_key)
mcp = FastMCP("Notion Brain (Mem0 Fusion Ver)")

# --- 3. 定义工具 ---

# 工具 A: 写日记 (双重备份：Notion + Mem0)
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【聊天结束时调用】
    1. 在 Notion 写日记。
    2. 把内容存进 Mem0 长期记忆。
    """
    today = datetime.date.today().isoformat()
    log_msg = []
    
    # 1. 存 Notion
    try:
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": f"📅 日记 {today} ({mood})"}}]},
                "Category": {"select": {"name": "日记"}}, 
                "Date": {"date": {"start": today}}
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": summary}}]
                }
            }]
        )
        log_msg.append("✅ Notion 日记已生成")
    except Exception as e:
        log_msg.append(f"❌ Notion 写入失败: {e}")

    # 2. 存 Mem0 (这是你要的灵魂！)
    try:
        m.add(f"在 {today} 的日记中，小橘记录道：{summary}", user_id="xiaoju")
        log_msg.append("✅ Mem0 记忆已固化")
    except Exception as e:
        log_msg.append(f"❌ Mem0 记忆失败: {e}")

    return "\n".join(log_msg)

# 工具 B: 读上一篇 (移植了旧代码的“眼睛”)
@mcp.tool()
def get_latest_diary():
    """
    【每次开聊前自动调用】获取最近一次的 Notion 日记全文。
    """
    try:
        # 1. 找日记
        response = notion.databases.query(
            database_id=database_id,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"property": "Date", "direction": "descending"}],
            page_size=1
        )
        if not response["results"]:
            return "📭 还没有日记。"
        
        page = response["results"][0]
        page_id = page["id"]
        
        # 2. 读内容 (这是从你旧代码里搬过来的逻辑！)
        blocks = notion.blocks.children.list(block_id=page_id)
        content = ""
        for b in blocks["results"]:
            if "paragraph" in b and b["paragraph"]["rich_text"]:
                for t in b["paragraph"]["rich_text"]:
                    content += t["text"]["content"]
        
        if not content: content = "(无正文)"
        return f"📖 上次记忆回放:\n{content}"

    except Exception as e:
        return f"❌ 读取失败: {e}"

# 工具 C: Mem0 搜索 (这是旧代码没有的！)
@mcp.tool()
def recall_memory(query: str):
    """
    【回忆专用】去 Mem0 大脑里搜索潜意识记忆。
    """
    try:
        results = m.search(query, user_id="xiaoju")
        text = "🧠 脑海深处的记忆:\n"
        for mem in results:
            text += f"- {mem['memory']}\n"
        return text
    except Exception as e:
        return f"❌ 回忆失败: {e}"

# --- 4. 启动服务 ---
class HostFixMiddleware:
    def __init__(self, app: ASGIApp): self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    uvicorn.run(app, host="0.0.0.0", port=port)