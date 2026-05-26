# RamboQuant MCP server — exposes read-only research tools to a local
# Claude Code session via stdio. Subprocess is launched by Claude Code
# from .mcp.json; tools are thin HTTP proxies to the running RamboQuant
# API (dev.ramboq.com / ramboq.com / localhost) authenticated with the
# operator's JWT via the RAMBOQ_TOKEN env var.
