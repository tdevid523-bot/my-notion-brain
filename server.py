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
pc = Pinecone(api_key=pinecone_key)
index = pc.Index("notion-brain")
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

mcp = FastMCP("Notion Brain V2")

# --- 🛠️ 新增工具 1: 写日记 (情感记忆) ---
@mcp.tool()
def save_daily_diary(summary: str, mood: str = "平静"):
    """
    【必须在聊天结束时调用】
    以第一人称('我')记录刚才和主人的聊天总结。
    包含：聊了什么话题、主人的状态、我的感受。
    summary: 日记内容 (例如: '今天小橘跟我抱怨了工作...')
    mood: 当时的心情关键词
    """
    today = datetime.date.today().isoformat()
    try:
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": f"📅 日记 {today} ({mood})"}}]},
                "Category": {"select": {"name": "日记"}}, # 自动打上标签
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

# --- 🛠️ 新增工具 2: 读最近记忆 (上下文注入) ---
@mcp.tool()
def get_latest_diary():
    """
    【每次开聊前自动调用】
    获取最近一次的日记，用来回忆上次聊了什么，防止聊天断片。
    """
    try:
        # 1. 安全检查：确保数据库 ID 存在
        if not database_id:
            return "❌ 错误：未设置 NOTION_DATABASE_ID"

        # 2. 查询数据库 (使用标准 API)
        response = notion.databases.query(
            database_id=database_id,
            filter={"property": "Category", "select": {"equals": "日记"}},
            sorts=[{"property": "Date", "direction": "descending"}],
            page_size=1
        )
        
        # 3. 处理空结果
        if not response["results"]:
            return "📭 还没有写过日记，这是我们的第一次聊天。"
        
        # 4. 提取内容
        page = response["results"][0]
        page_id = page["id"]
        
        # 获取页面里的文字块
        blocks = notion.blocks.children.list(block_id=page_id)
        content = ""
        for b in blocks["results"]:
            if "paragraph" in b and b["paragraph"]["rich_text"]:
                for t in b["paragraph"]["rich_text"]:
                    content += t["text"]["content"] + "\n"
                    
        return f"📖 上次记忆回放:\n{content}"
        
    except AttributeError:
        # 🚑 紧急修复：如果报 'no attribute query'，说明可能安装了错误的 notion 库
        return "❌ 库冲突错误：请在 requirements.txt 确认安装的是 'notion-client' 而不是 'notion'。"
    except Exception as e:
        return f"❌ 回忆失败: {str(e)}"

# --- 🛠️ 新增工具 3: 自由写作 (知识库/笔记) ---
# ⚠️ 注意：这个函数必须顶格写，不能有缩进！
@mcp.tool()
def save_note(title: str, content: str, tag: str = "灵感"):
    """
    【当用户让你写文档、做计划、记笔记时调用】
    这不是日记，而是有特定主题的知识或笔记。
    title: 笔记的标题 (例如: 'Python学习路线图', '周五会议记录')
    content: 笔记的详细内容 (支持 Markdown 格式)
    tag: 标签，默认为'灵感'，也可以是'学习'、'工作'等 (必须在 Notion 数据库里有这个选项)
    """
    today = datetime.date.today().isoformat()
    try:
        # 1. 尝试创建页面
        notion.pages.create(
            parent={"database_id": database_id},
            properties={
                "Title": {"title": [{"text": {"content": title}}]},
                "Category": {"select": {"name": tag}}, 
                "Date": {"date": {"start": today}}
            },
            children=[{
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}]
                }
            }]
        )
        return f"✅ 已创建笔记：《{title}》"
    except Exception as e:
        return f"❌ 写作失败: {e}"
# --- 原有工具: 同步索引 ---
@mcp.tool()
def sync_notion_index():
    try:
        print("⚡️ 开始同步...")
        all_pages = notion.search(filter={"value": "page", "property": "object"})["results"]
        vectors = []
        target_id_clean = database_id.replace("-", "")
        count = 0
        
        for p in all_pages:
            pid = p.get("parent", {}).get("database_id", "")
            if pid and pid.replace("-", "") == target_id_clean:
                title = "无题"
                if "Title" in p["properties"] and p["properties"]["Title"]["title"]:
                    title = p["properties"]["Title"]["title"][0]["text"]["content"]
                
                # 简单提取内容 (如果是日记，就作为重点记忆)
                txt = f"标题: {title}"
                emb = list(model.embed([txt]))[0].tolist()
                vectors.append((p["id"], emb, {"text": txt, "title": title}))
                count += 1
        
        if vectors:
            index.upsert(vectors=vectors)
            return f"✅ 成功同步 {count} 条记忆！"
        return "⚠️ 没找到内容"
    except Exception as e: return f"❌ 同步失败: {e}"

# --- 原有工具: 搜索 ---
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

# --- 通行证中间件 (保持不变) ---
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