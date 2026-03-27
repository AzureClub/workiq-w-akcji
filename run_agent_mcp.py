"""
Local MCP orchestrator: Work IQ MCP server ↔ Azure AI Foundry agent.

Flow:
  1. Starts workiq MCP server (stdio) locally
  2. Discovers available tools via MCP protocol
  3. Sends user question + tool definitions to Foundry model
  4. When model requests a tool call → forwards to MCP → returns result
  5. If MCP tool fails → falls back to workiq CLI
  6. Loops until model produces final text answer

All tool calls are visible in console output.
"""

import asyncio
import json
import shutil
import subprocess
import sys
from datetime import timedelta

from mcp import StdioServerParameters, ClientSession
from mcp.client.stdio import stdio_client
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

ENDPOINT = "https://agent-intercity-resource.services.ai.azure.com/api/projects/agent-intercity"
MODEL = "gpt-4.1"
INSTRUCTIONS = (
    "Jesteś pomocnym asystentem biurowym. "
    "Używaj dostępnych narzędzi (ask_work_iq) do odpowiadania na pytania "
    "o kalendarz, maile, spotkania, dokumenty i inne dane z Microsoft 365. "
    "Odpowiadaj po polsku."
)


def find_workiq_cmd() -> str:
    """Locate workiq executable."""
    found = shutil.which("workiq")
    if found:
        return found
    import os
    fallback = os.path.join(
        os.environ.get("APPDATA", ""), "npm", "workiq.cmd"
    )
    if os.path.exists(fallback):
        return fallback
    raise FileNotFoundError("workiq not found. Install with: npm install -g @microsoft/workiq")


def call_workiq_cli(question: str) -> str:
    """Fallback: call workiq via CLI when MCP tool fails."""
    workiq_path = find_workiq_cmd()
    result = subprocess.run(
        [workiq_path, "ask", "-q", question],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        return f"Error: {result.stderr}"
    return result.stdout.strip()


def mcp_tools_to_openai(mcp_tools) -> list[dict]:
    """Convert MCP tool definitions to OpenAI function tool format."""
    openai_tools = []
    for tool in mcp_tools:
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        openai_tools.append({
            "type": "function",
            "name": tool.name,
            "description": tool.description or "",
            "parameters": schema,
        })
    return openai_tools


async def run(question: str):
    workiq_path = find_workiq_cmd()
    print(f"🔌 Work IQ MCP: {workiq_path}")

    server_params = StdioServerParameters(
        command=workiq_path,
        args=["mcp"],
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream=read_stream,
            write_stream=write_stream,
            read_timeout_seconds=timedelta(seconds=120),
        ) as mcp:
            # Initialize MCP session
            init = await mcp.initialize()
            print(f"✅ MCP connected: {init.serverInfo.name} v{init.serverInfo.version}")

            # Discover tools
            tools_result = await mcp.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            print(f"🔧 Tools: {tool_names}\n")

            openai_tools = mcp_tools_to_openai(tools_result.tools)

            # Connect to Foundry
            project = AIProjectClient(
                endpoint=ENDPOINT, credential=DefaultAzureCredential()
            )
            client = project.get_openai_client()

            # Initial request
            print(f"💬 Pytanie: {question}\n")
            print("=" * 60)

            response = client.responses.create(
                model=MODEL,
                instructions=INSTRUCTIONS,
                input=[{"role": "user", "content": question}],
                tools=openai_tools,
            )

            # Tool calling loop
            iteration = 0
            while True:
                iteration += 1
                tool_calls = [
                    item for item in response.output
                    if item.type == "function_call"
                ]

                if not tool_calls:
                    break

                print(f"\n🔄 Iteracja {iteration}: {len(tool_calls)} tool call(s)")

                tool_outputs = []
                for tc in tool_calls:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                    print(f"  → {tc.name}({json.dumps(args, ensure_ascii=False)[:200]})")

                    # Try MCP first
                    mcp_result = await mcp.call_tool(
                        name=tc.name,
                        arguments=args,
                        read_timeout_seconds=timedelta(seconds=120),
                    )

                    result_text = "\n".join(
                        block.text
                        for block in mcp_result.content
                        if hasattr(block, "text")
                    )

                    # Check if MCP returned an error payload — fall back to CLI
                    mcp_failed = mcp_result.isError
                    if not mcp_failed and tc.name == "ask_work_iq":
                        try:
                            payload = json.loads(result_text)
                            if payload.get("error") or payload.get("response") is None:
                                mcp_failed = True
                        except (json.JSONDecodeError, TypeError):
                            pass

                    if mcp_failed and tc.name == "ask_work_iq":
                        question_arg = args.get("question", "")
                        print(f"  ⚠️  MCP failed, falling back to CLI...")
                        result_text = call_workiq_cli(question_arg)
                        status = "🔄 CLI fallback"
                    else:
                        status = "❌ ERROR" if mcp_result.isError else "✅ MCP"

                    preview = result_text[:150].replace("\n", " ")
                    print(f"  ← {status}: {preview}...")

                    tool_outputs.append({
                        "type": "function_call_output",
                        "call_id": tc.call_id,
                        "output": result_text,
                    })

                # Send tool results back to model
                response = client.responses.create(
                    model=MODEL,
                    instructions=INSTRUCTIONS,
                    input=tool_outputs,
                    tools=openai_tools,
                    previous_response_id=response.id,
                )

            # Final answer
            print("\n" + "=" * 60)
            print(f"\n📋 Odpowiedź agenta:\n")
            print(response.output_text)


def main():
    question = (
        " ".join(sys.argv[1:])
        if len(sys.argv) > 1
        else "Jakie mam dzisiaj spotkania w kalendarzu? Podaj listę z godzinami."
    )
    asyncio.run(run(question))


if __name__ == "__main__":
    main()
