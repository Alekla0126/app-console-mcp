# The three services as one MCP server on stdio. Credentials come from environment variables
# (see README, "All settings"); without them the server still starts and lists its tools.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
# The official MCP Registry checks this label to know the image belongs to server.json's server.
LABEL io.modelcontextprotocol.server.name="io.github.Alekla0126/app-console-mcp"
LABEL org.opencontainers.image.source="https://github.com/Alekla0126/app-console-mcp"
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv pip install --system --no-cache .
ENTRYPOINT ["appconsole", "mcp", "all"]
