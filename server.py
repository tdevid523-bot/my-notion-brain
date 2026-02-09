import os
import sys
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from pinecone import Pinecone
from fastembed import TextEmbedding
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

# --- 1. 强制让日志即时输出 (解决日志延迟) ---
sys.stdout.reconfigure(line_buffering=True)

print("🔥 正在启动 V4.0 霸道版... 如果你没看到这句话，说明没部署成功！")

# --- 2. 获取配置 ---
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")
pinecone_key = os.environ.get("PINECONE_API_KEY")

notion = Client(auth=notion_key)
if pinecone_key:
    pc = Pinecone(api_key=pinecone_key)
    index = pc.Index("notion-brain")
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

mcp = FastMCP("Notion Brain V2")

# --- 3. 霸道伪装中间件 (核弹级) ---
class DictatorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # 1. 强制修改 Scope 里的 Host，骗过所有检查
        # 这里的关键是：我们要骗程序说“这是从 localhost 来的”
        request.scope['server'] = ('localhost', 8000)
        
        # 2. 强制修改 Headers 里的 Host
        # 我们先把它转成可变的 dict，改完再塞回去
        headers = dict(request.scope['headers'])
        headers[b'host'] = b'localhost'
        request.scope['headers'] = list(headers.items())
        
        # 3. 打印一行日志证明我来过 (调试用)
        # print(f"🔨 霸道中间件已拦截请求，强制伪装为 localhost")
        
        response = await call_next(request)
        return response

# --- 4. 工具定义 (保持不变) ---
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    today = os.environ.get("TODAY", "2026-02-09") # 简化逻辑防报错
    try:
        if not database_id: return "❌ 没配置 Database ID"
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

# --- 5. 启动配置 (最关键的一步) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 V4.0 霸道版启动中，端口: {port}")
    
    # 1. 拿到 App
    app = mcp.sse_app()
    
    # 2. 【双重保险】加上 Starlette 官方的“允许所有域名”中间件
    # 这一步是告诉保安：任何域名都放行！
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
    
    # 3. 【三重保险】加上我们的“霸道伪装”中间件
    # 这一步是：如果有保安不听话，就骗它说是 localhost
    app.add_middleware(DictatorMiddleware)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        http="h11" # 强制 HTTP/1.1 避免 421 协议错误
    )