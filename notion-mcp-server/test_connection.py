import asyncio
from mcp.client.fastmcp import Client

async def test_write_notion_page():
    client = Client()
    try:
        response = await client.call(
            "write_notion_page",
            title="Hello MCP Test",
            content="Hello MCP! 这是来自 Cline 的第一次测试"
        )
        print("Notion API Response:", response)
    except Exception as e:
        print(f"Error calling write_notion_page: {e}")

if __name__ == "__main__":
    asyncio.run(test_write_notion_page())
