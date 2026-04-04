"""
TDD Test: MCP Server — Tool Registry (the nervous system spine).
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_register_tool_with_docstring():
    from core.mcp_server import ToolRegistry
    reg = ToolRegistry()

    @reg.register
    def add(a: int = 0, b: int = 0):
        """Add two numbers."""
        return a + b

    assert "add" in reg._tools
    assert len(reg._schemas) == 1
    assert reg._schemas[0]["name"] == "add"


def test_register_rejects_missing_docstring():
    from core.mcp_server import ToolRegistry
    reg = ToolRegistry()
    try:
        @reg.register
        def bad_tool():
            pass
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_execute_registered_tool():
    from core.mcp_server import ToolRegistry
    reg = ToolRegistry()

    @reg.register
    def multiply(a: int = 1, b: int = 1):
        """Multiply two numbers."""
        return a * b

    result = reg.execute_tool("multiply", {"a": 3, "b": 7})
    assert result == "21"


def test_execute_nonexistent_tool():
    from core.mcp_server import ToolRegistry
    reg = ToolRegistry()
    result = reg.execute_tool("ghost_tool", {})
    assert "ERROR" in result
    assert "ghost_tool" in result


def test_execute_handles_crash():
    from core.mcp_server import ToolRegistry
    reg = ToolRegistry()

    @reg.register
    def crasher():
        """I always crash."""
        raise RuntimeError("boom")

    result = reg.execute_tool("crasher", {})
    assert "ERROR" in result
    assert "boom" in result


def test_get_schemas_format():
    from core.mcp_server import ToolRegistry
    reg = ToolRegistry()

    @reg.register
    def ping(msg: str = "hi"):
        """Ping the system."""
        return f"pong: {msg}"

    schemas = reg.get_schemas()
    assert len(schemas) == 1
    s = schemas[0]
    assert s["name"] == "ping"
    assert "description" in s
    assert "input_schema" in s
    assert s["input_schema"]["type"] == "object"
