import os
from dotenv import load_dotenv
from notion_client import Client

# 1. 加载配置
load_dotenv()
token = os.getenv("NOTION_API_KEY")
page_id = os.getenv("NOTION_PAGE_ID")

print(f"🔑 Key前缀: {token[:5]}...")
print(f"📄 Page ID: {page_id}")

# 2. 直接开始测试
try:
    client = Client(auth=token)
    
    # 这一步是为了确认连接没问题
    user = client.users.me()
    print(f"✅ 连接成功！机器人名称: {user.get('name', 'Unknown')}")

    # 3. 写入测试
    print(">>> 正在尝试写入内容...")
    client.blocks.children.append(
        block_id=page_id,
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": "🚀 成功了！这是来自 notion-bot 的第一条消息！"
                            }
                        }
                    ]
                }
            }
        ]
    )
    print("\n✨✨✨ 写入成功！快去你的 Notion 页面看看！ ✨✨✨")

except Exception as e:
    print("\n❌ 发生错误:")
    print(e)