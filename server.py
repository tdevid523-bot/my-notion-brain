import os
import datetime
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from mem0 import Memory
from dotenv import load_dotenv
from starlette.types import ASGIApp, Scope, Receive, Send

# --- 积木 1: 初始化配置 ---
load_dotenv()

# 获取 Notion 的钥匙
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")

# --- 积木 2: 组装云端大脑 (反代 + Qdrant 版) ---
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
            "model": "gpt-4o-mini", # 你的反代支持的模型名
            "temperature": 0.1,
            "max_tokens": 2000,
            # 👇 让它走你的反代
            "openai_base_url": os.environ.get("OPENAI_BASE_URL"), 
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": "text-embedding-3-small",
            # 👇 嵌入也要走反代
            "openai_base_url": os.environ.get("OPENAI_BASE_URL"),
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
    }
}

print(f"🧠 正在连接云端大脑 (反代地址: {os.environ.get('OPENAI_BASE_URL')})...")

# 初始化所有服务 (只做一次)
m = Memory.from_config(config_cloud)
notion = Client(auth=notion_key)
mcp = FastMCP("Notion Brain V3 (Cloud)")

# --- 积木 3: 定义工具 (AI 能做什么) ---

# 工具 A: 写日记
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【聊天结束时调用】
    1. 在 Notion 创建一篇日记。
    2. 将日记内容存入 Mem0 长期记忆。
    summary: 日记内容
    mood: 当时的心情
    """
    today = datetime.date.today().isoformat()
    log_msg = []
    
    # 1. 存入 Notion
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

    # 2. 存入 Mem0
    try:
        m.add(f"在 {today} 的日记中，小橘记录道：{summary}", user_id="xiaoju")
        log_msg.append("✅ Mem0 记忆已固化")
    except Exception as e:
        log_msg.append(f"❌ Mem0 记忆失败: {e}")

    return "\n".join(log_msg)

# 工具 B: 智能回忆
@mcp.tool()
def recall_memory(query: str):
    """
    【需要回忆细节时调用】去大脑里搜索相关的记忆。
    query: 你想知道什么？
    """
    try:
        results = m.search(query, user_id="xiaoju")
        if not results:
            return "📭 大脑里好像没有关于这个的记忆。"
            
        text = "🧠 脑海中浮现的记忆:\n"
        for mem in results:
            text += f"- {mem['memory']}\n"
        return text
    except Exception as e:
        return f"❌ 回忆失败: {e}"

# 工具 C: 读上一篇 (能读正文版)
@mcp.tool()
def get_latest_diary():
    """
    【每次开聊前自动调用】获取最近一次的 Notion 日记全文。
    """
    try:
        # 第一步：先找到最后一篇日记 (拿到 ID)
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
        date = page["properties"]["Date"]["date"]["start"]
        title_obj = page["properties"]["Title"]["title"]
        title_text = title_obj[0]["text"]["content"] if title_obj else "无标题"

        # 第二步：读取正文 Block
        blocks = notion.blocks.children.list(block_id=page_id)
        content = ""
        for block in blocks["results"]:
            if "paragraph" in block and block["paragraph"]["rich_text"]:
                text = block["paragraph"]["rich_text"][0]["text"]["content"]
                content += text + "\n"
        
        if not content:
            content = "(这篇日记好像没有正文内容)"

        return f"📖 上次日记 ({date} - {title_text}):\n\n{content}"

    except Exception as e:
        return f"❌ 读取失败: {e}"

# --- 积木 4: 启动服务器 (补全了缺失的中间件类) ---

# 👇 这就是之前缺失的类定义！
class HostFixMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            # 强制修改 Host 头，骗过 Render 的检查
            headers = dict(scope.get("headers", []))
            headers[b"host"] = b"localhost:8000"
            scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # 这里使用上面定义的 HostFixMiddleware
    app = HostFixMiddleware(mcp.sse_app())
    uvicorn.run(app, host="0.0.0.0", port=port)