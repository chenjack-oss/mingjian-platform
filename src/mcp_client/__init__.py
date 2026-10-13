"""MCP 客户端适配层：把 MCP Server 的工具转成 LangChain StructuredTool。

目录刻意叫 `mcp_client` 而不是 `mcp`：
    与官方 SDK 包 `mcp` 同名后，`import mcp` 解析到哪个取决于 sys.path，
    在仓库根目录跑是一种结果、进到 src/ 跑是另一种 —— 属于定时炸弹，不值得赌。
"""
