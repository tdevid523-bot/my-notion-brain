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

# 容错处理：确保 Key 存在，否则打印警告
if not notion_key:
    print("❌ 严重错误: 缺少 NOTION_API_KEY！")
else:
    print("✅ Notion Key 已检测到")

notion = Client(auth=notion_key)

if pinecone_key:
    pc = Pinecone(api_key=pinecone_key)
    index = pc.Index("notion-brain")
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    print("✅ Pinecone 搜索已就绪")
else:
    print("⚠️ 警告: 没有 PINECONE_API_KEY，搜索功能将不可用")

# 定义 MCP 服务
mcp = FastMCP("Notion Brain V2")

# --- 🛠️ 工具 1: 写日记 ---
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【聊天结束时调用】记录聊天总结和心情。
    summary: 聊天内容的总结
    mood: 当时的心情
    """
    today = datetime.date.today().isoformat()
    try:
        if not database_id:
            return "❌ 错误: 没配置 Notion Database ID"

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
        return f"❌ 写日记失败: {str(e)}"

# --- 🛠️ 工具 2: 读最近记忆 ---
@mcp.tool()
def get_latest_diary():
    """
    【开聊前调用】获取最近一次的日记，如果没有则返回空。
    """
    try:
        if not database_id:
            return "❌ 错误: 没配置 Notion Database ID"

        response = notion.databases.query(
            database_id=database_id,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"property": "Date", "direction": "descending"}],
            page_size=1
        )
        if not response["results"]:
            return "📭 还没有写过日记。"
        
        page = response["results"][0]
        # 读取 Block 内容
        blocks = notion.blocks.children.list(block_id=page["id"])
        content = ""
        for b in blocks["results"]:
            if "paragraph" in b and b["paragraph"]["rich_text"]:
                for t in b["paragraph"]["rich_text"]:
                    content += t["text"]["content"]
        return f"📖 上次记忆回放:\n{content}"
    except Exception as e:
        return f"❌ 回忆失败: {str(e)}"

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

# --- 🚀 启动部分 (关键修改) ---
if __name__ == "__main__":
    # 获取 Render 提供的端口，默认 7860
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 服务启动中，监听端口: {port}")
    
    # 【核心修复】：
    # 1. host="0.0.0.0": 允许外部访问
    # 2. proxy_headers=True: 告诉 Uvicorn 它是跑在代理后面的
    # 3. forwarded_allow_ips="*": 【最重要】信任 Render 的 IP，解决 421 错误
    uvicorn.run(
        mcp.sse_app(), 
        host="0.0.0.0", 
        port=port, 
        proxy_headers=True,
        forwarded_allow_ips="*" 
    )