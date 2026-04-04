# core/mcp_server.py
"""
MCP Server — DevClaw's central nervous system spine.

The ToolRegistry is the physical bridge between the LLM's "thoughts"
and the real world's "actions". Every tool must be registered here
before the LLM can see or use it.

Contract: every tool MUST have a docstring (the physical contract).
No docstring = rejected at registration.
"""
import json
import inspect
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [NERVOUS_SYSTEM] %(message)s')


class ToolRegistry:
    def __init__(self):
        # Physical toolbox: name -> real Python function
        self._tools = {}
        # LLM cognitive map: JSON Schema list for tool_calling
        self._schemas = []

    def register(self, func):
        """Register a physical function as an LLM-callable tool."""
        name = func.__name__
        docstring = inspect.getdoc(func)

        if not docstring:
            raise ValueError(f"Tool {name} missing physical contract (docstring). Registration denied.")

        self._tools[name] = func

        # Build Claude-compatible tool schema
        schema = {
            "name": name,
            "description": docstring,
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
        self._schemas.append(schema)
        logging.info(f"Neuron bridged: {name}")
        return func

    def get_schemas(self):
        """Return tool schemas for LLM API tools parameter."""
        return self._schemas

    def execute_tool(self, tool_name: str, arguments: dict):
        """Cold-blooded execution layer. Only legal calls execute."""
        if tool_name not in self._tools:
            return f"[ERROR] Fatal intercept: tool '{tool_name}' does not exist."

        try:
            logging.info(f"Muscle contraction -> {tool_name}({arguments})")
            result = self._tools[tool_name](**arguments)
            return str(result)
        except Exception as e:
            return f"[ERROR] Physical execution crash: {str(e)}\nCheck parameters and retry."


# ── Self-test ──
if __name__ == "__main__":
    registry = ToolRegistry()

    @registry.register
    def system_ping(message: str = "ping"):
        """Probe if host machine is alive. Returns system time and echo."""
        import datetime
        return f"PONG. Time: {datetime.datetime.now()}, Echo: {message}"

    output = registry.execute_tool("system_ping", {"message": "Hello from LLM Brain"})
    print("\n[Host Feedback]:", output)
    print("\n[LLM Schema]:", json.dumps(registry.get_schemas(), ensure_ascii=False, indent=2))
