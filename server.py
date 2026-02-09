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
print("⏳ 正在初始化 V3 最终版...")
notion = Client(auth=notion_key)

if pinecone_key:
    pc = Pinecone(api_key=pinecone_key)
    index = pc.Index("notion-brain")
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
else:
    print("⚠️ 警告: 没有 PINECONE_API_KEY")

mcp = FastMCP("Notion Brain V2")

# --- 🛠️ 强力伪装中间件 (带日志版) ---
class ForceLocalhostMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            
            # 🛑 打印日志：证明新代码生效了
            original_host = headers.get(b"host", b"unknown").decode()
            # print(f"🔍 收到请求，原始 Host: {original_host}，正在伪装成 localhost...")
            
            # 强制修改 Host 头
            headers[b"host"] = b"localhost"
            scope["headers"] = list(headers.items())
            
        await self.app(scope, receive, send)

# --- 🛠️ 工具部分 ---
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    today = datetime.date.today().isoformat()
    try:
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": f"📅 日记 {today} ({mood})"}}]},
                "Category": {"select": {"name": "日记"}}, 
                "Date": {"date": {"start": today}}
            },
            children=[{"object":"block","type":"paragraph","paragraph":{"rich_text":[{"type":"text","text":{"content":summary}}]}}]
        )
        return "✅ 日记已写好！"
    except Exception as e: return f"❌ 失败: {e}"

@mcp.tool()
def get_latest_diary():
    try:
        response = notion.databases.query(
            database_id=database_id,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"property": "Date", "direction": "descending"}],
            page_size=1
        )
        if not response["results"]: return "📭 无日记"
        page = response["results"][0]
        blocks = notion.blocks.children.list(block_id=page["id"])
        content = ""
        for b in blocks["results"]:
            if "paragraph" in b and b["paragraph"]["rich_text"]:
                for t in b["paragraph"]["rich_text"]: content += t["text"]["content"]
        return f"📖 记忆回放:\n{content}"
    except Exception as e: return f"❌ 失败: {e}"

@mcp.tool()
def search_memory_semantic(query: str):
    if not pinecone_key: return "❌ Pinecone 未配置"
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        ans = "Found:\n"
        for m in res["matches"]: ans += f"- {m['metadata'].get('text','')} ({m['score']:.2f})\n"
        return ans
    except Exception as e: return f"❌ 失败: {e}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 V3 服务启动中，端口: {port}")
    
    # 这里的顺序极其重要！
    app = mcp.sse_app() 
    app = ForceLocalhostMiddleware(app) # 👈 必须套在这里
    
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")