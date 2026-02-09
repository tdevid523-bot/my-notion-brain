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
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")

# --- 2. 组装云端大脑 (Mem0 + Qdrant) ---
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
try:
    m = Memory.from_config(config_cloud)
except Exception as e:
    print(f"⚠️ Mem0 连接警告 (如果不影响启动可忽略): {e}")
    m = None # 避免启动崩溃

notion = Client(auth=notion_key)
mcp = FastMCP("Notion Brain (Fusion Ver)")

# --- 3. 定义工具 ---

@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【写日记】同时存入 Notion 和 Mem0 记忆库。
    summary: 日记内容
    mood: 心情
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
        log_msg.append(f"❌ Notion 失败: {e}")

    # 2. 存 Mem0
    if m:
        try:
            m.add(f"在 {today} 的日记中，小橘记录道：{summary}", user_id="xiaoju")
            log_msg.append("✅ Mem0 记忆已固化")
        except Exception as e:
            log_msg.append(f"❌ Mem0 失败: {e}")
    else:
        log_msg.append("⚠️ Mem0 未连接，仅存了 Notion")

    return "\n".join(log_msg)

@mcp.tool()
def get_latest_diary():
    """
    【读日记】读取上一篇日记的全文（含正文）。
    """
    try:
        # 1. 找最近一篇
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
        
        # 2. 读正文 (这里修复了之前只读标题的问题)
        blocks = notion.blocks.children.list(block_id=page_id)
        content = ""
        for b in blocks["results"]:
            # 兼容各种文本块
            if "paragraph" in b and b["paragraph"]["rich_text"]:
                for t in b["paragraph"]["rich_text"]:
                    content += t["text"]["content"]
            # 如果是其他类型也尝试读取（可选）
        
        if not content: content = "(无正文内容)"
        return f"📖 上次记忆回放:\n{content}"

    except Exception as e:
        return f"❌ 读取失败: {e}"

@mcp.tool()
def recall_memory(query: str):
    """
    【搜索记忆】从 Mem0 搜索相关记忆。
    """
    if not m: return "❌ Mem0 大脑未连接"
    try:
        results = m.search(query, user_id="xiaoju")
        text = "🧠 脑海浮现:\n"
        for mem in results:
            text += f"- {mem['memory']}\n"
        return text
    except Exception as e:
        return f"❌ 搜索失败: {e}"

# --- 4. 启动服务 (关键修复！) ---
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