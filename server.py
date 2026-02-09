import os
import datetime
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from pinecone import Pinecone
from fastembed import TextEmbedding

# 1. 获取配置
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")
pinecone_key = os.environ.get("PINECONE_API_KEY")

# 2. 初始化
print("⏳ 正在初始化 V2 进化版服务...")
# 注意：如果环境变量没配对，这里会报错导致服务起不来
# 建议在 Render 仪表盘检查 Environment Variables
if not notion_key or not pinecone_key:
    print("⚠️ 警告：检测到 API Key 缺失！服务可能无法正常工作。")

notion = Client(auth=notion_key)
pc = Pinecone(api_key=pinecone_key)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

mcp = FastMCP("Notion Brain V2")

# --- 🛠️ 工具 1: 写日记 ---
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【必须在聊天结束时调用】
    以第一人称('我')记录刚才和主人的聊天总结。
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
        return "✅ 日记已写好！记忆已固化。"
    except Exception as e:
        return f"❌ 写日记失败: {e}"

# --- 🛠️ 工具 2: 读最近记忆 ---
@mcp.tool()
def get_latest_diary():
    """
    【每次开聊前自动调用】
    获取最近一次的日记。
    """
    try:
        response = notion.databases.query(
            database_id=database_id,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"property": "Date", "direction": "descending"}],
            page_size=1
        )
        if not response["results"]:
            return "📭 还没有写过日记，这是我们的第一次聊天。"
        
        page = response["results"][0]
        # 获取内容逻辑...
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
    try:
        vec = list(model.embed([query]))[0].tolist()
        res = index.query(vector=vec, top_k=3, include_metadata=True)
        ans = "Found:\n"
        for m in res["matches"]:
            ans += f"- {m['metadata'].get('text','')} (相似度 {m['score']:.2f})\n"
        return ans
    except Exception as e: return f"❌ 搜索失败: {e}"

# --- 🚀 启动部分 (已修改) ---
if __name__ == "__main__":
    # Render 会自动注入 PORT 环境变量，通常是 10000
    # 我们这里默认设为 7860 以防万一
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 服务正在启动，监听端口: {port}")
    
    # ❌ 删除了 HostFixMiddleware
    # 直接运行 mcp.sse_app()
    uvicorn.run(mcp.sse_app(), host="0.0.0.0", port=port)