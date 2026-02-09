import os
import datetime
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.types import ASGIApp, Scope, Receive, Send

# 1. 获取配置
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")
pinecone_key = os.environ.get("PINECONE_API_KEY")

# 2. 初始化
print("⏳ 正在初始化 V2 进化版服务...")
notion = Client(auth=notion_key)

# 容错处理：如果没有 Pinecone Key，就不崩，只打印警告
if pinecone_key:
    pc = Pinecone(api_key=pinecone_key)
    index = pc.Index("notion-brain")
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
else:
    print("⚠️ 警告: 没有检测到 PINECONE_API_KEY，搜索功能将不可用")

mcp = FastMCP("Notion Brain V2")

# --- 🛠️ 中间件: 解决 421 Invalid Host Header ---
class HostFixMiddleware:
    """
    Render 发来的请求 Host 是 'xxx.onrender.com'。
    但 MCP/Starlette 默认可能只认 'localhost'。
    这个中间件把 Host 头强行改为 'localhost'，欺骗程序让它接客。
    """
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            # 强制伪装成 localhost:8000
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

# --- 🛠️ 工具 1: 写日记 ---
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【聊天结束时调用】记录聊天总结和心情。
    """
    today = datetime.date.today().isoformat()
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
        return "✅ 日记已写好！"
    except Exception as e:
        return f"❌ 写日记失败: {e}"

# --- 🛠️ 工具 2: 读最近记忆 ---
@mcp.tool()
def get_latest_diary():
    """
    【开聊前调用】获取最近一次的日记。
    """
    try:
        response = notion.databases.query(
            database_id=database_id,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"property": "Date", "direction": "descending"}],
            page_size=1
        )
        if not response["results"]:
            return "📭 还没有写过日记。"
        
        page = response["results"][0]
        blocks = notion.blocks.children.list(block_id=page["id"])
        content = ""
        for b in blocks["results"]:
            if "paragraph" in b and b["paragraph"]["rich_text"]:
                for t in b["paragraph"]["rich_text"]:
                    content += t["text"]["content"]
        return f"📖 上次记忆回放:\n{content}"
    except Exception as e:
        return f"❌ 回忆失败: {e}"

# --- 🛠️ 工具 3: 搜索 ---
@mcp.tool()
def search_memory_semantic(query: str):
    if not pinecone_key: return "❌ Pinecone 未配置，无法搜索"
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        ans = "Found:\n"
        for m in res["matches"]:
            ans += f"- {m['metadata'].get('text','')} (相似度 {m['score']:.2f})\n"
        return ans
    except Exception as e: return f"❌ 搜索失败: {e}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 服务启动中，端口: {port}")
    
    # 关键修改：套上中间件
    app = HostFixMiddleware(mcp.sse_app())
    
    # 关键修改：proxy_headers=True 让它信任 Render
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True)