import os
import datetime
import uvicorn
from mcp.server.fastmcp import FastMCP
from notion_client import Client
from mem0 import Memory
from dotenv import load_dotenv
from starlette.types import ASGIApp, Scope, Receive, Send

os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"
# --- 积木 1: 初始化配置 ---
# 加载 .env 文件里的密码
load_dotenv()

# 获取 Notion 的钥匙
notion_key = os.environ.get("NOTION_API_KEY")
database_id = os.environ.get("NOTION_DATABASE_ID")

# --- 积木 2: 组装大脑 (Mem0 + Ollama) ---
# 这里告诉 Mem0："不要用 OpenAI，用我电脑上的 Ollama"
config_ollama = {
    "llm": {
        "provider": "ollama",
        "config": {
            "model": "qwen2.5",     # 刚才下载的聊天模型
            "temperature": 0.1,     # 0.1 代表严谨，不胡编乱造
            "max_tokens": 2000,
        }
    },
    "embedder": {
        "provider": "ollama",
        "config": {
            "model": "nomic-embed-text" # 刚才下载的嵌入模型
        }
    },
    "vector_store": {
        "provider": "qdrant",  # <--- 改成 qdrant
        "config": {
            "collection_name": "xiaoju_memory",
            "path": "local_mem0_db",  # 指定一个文件夹，这样重启后记忆还在！
        }
    }
}

print("🧠 正在连接本地 Ollama 大脑...")
# 初始化两个工具
m = Memory.from_config(config_ollama)  # 智能大脑
notion = Client(auth=notion_key)       # 日记本
mcp = FastMCP("Notion Brain V3 (Local)")


# --- 积木 3: 定义工具 (AI 能做什么) ---

# 工具 A: 写日记 (双重备份)
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【聊天结束时调用】
    1. 在 Notion 创建一篇日记（给主人看）。
    2. 将日记内容存入 Mem0 长期记忆（给 AI 记）。
    summary: 日记内容
    mood: 当时的心情
    """
    today = datetime.date.today().isoformat()
    log_msg = []
    
    # 1. 存入 Notion (看得见的日记)
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

    # 2. 存入 Mem0 (看不见的潜意识)
    try:
        # Mem0 会自动提取事实，比如 "User likes coding"
        m.add(f"在 {today} 的日记中，小橘记录道：{summary}", user_id="xiaoju")
        log_msg.append("✅ Mem0 记忆已固化")
    except Exception as e:
        log_msg.append(f"❌ Mem0 记忆失败: {e}")

    return "\n".join(log_msg)

# 工具 B: 智能回忆 (脑海搜索)
@mcp.tool()
def recall_memory(query: str):
    """
    【需要回忆细节时调用】
    去大脑里搜索相关的记忆。
    query: 你想知道什么？(例如 "小橘上次提到的项目痛点是什么？")
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

# 工具 C: 读上一篇 (上下文)
# --- 工具 C: 读上一篇 (升级版：能读正文) ---
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

        # 第二步：【关键】根据 ID 去读取页面里的“积木” (Blocks)
        # 这就是为什么之前只能看到题目，因为少了这个步骤
        blocks = notion.blocks.children.list(block_id=page_id)
        
        content = ""
        for block in blocks["results"]:
            # 检查这个积木是不是一段话 (paragraph)
            if "paragraph" in block and block["paragraph"]["rich_text"]:
                text = block["paragraph"]["rich_text"][0]["text"]["content"]
                content += text + "\n"
        
        if not content:
            content = "(这篇日记好像没有正文内容)"

        return f"📖 上次日记 ({date} - {title_text}):\n\n{content}"

    except Exception as e:
        return f"❌ 读取失败: {e}"

# --- 积木 4: 启动服务器 (固定写法) ---
# --- 积木 2: 组装云端大脑 (适配 Render) ---
# 我们改用 OpenAI + Qdrant Cloud，这样在 Render 上也能跑！
# --- 积木 2: 组装云端大脑 (反代专用版) ---
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
            "model": "gpt-4o-mini",  # 你的反代支持的模型名字
            "temperature": 0.1,
            "max_tokens": 2000,
            # 👇 关键：增加这一行，让它读你的反代地址
            "openai_base_url": os.environ.get("OPENAI_BASE_URL"), 
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": "text-embedding-3-small",
            # 👇 嵌入模型也要走反代 (如果你的反代支持的话)
            "openai_base_url": os.environ.get("OPENAI_BASE_URL"),
            "api_key": os.environ.get("OPENAI_API_KEY"),
        }
    }
}

print(f"🧠 正在连接云端大脑 (反代地址: {os.environ.get('OPENAI_BASE_URL')})...")
m = Memory.from_config(config_cloud)

notion = Client(auth=notion_key)       
mcp = FastMCP("Notion Brain V3 (Cloud)")
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app = HostFixMiddleware(mcp.sse_app())
    uvicorn.run(app, host="0.0.0.0", port=port)