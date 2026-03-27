import asyncio
import unittest

from fillform.mcp import list_tools


class McpToolSchemaTests(unittest.TestCase):
    def test_validate_form_supports_expected_values(self) -> None:
        tools = asyncio.run(list_tools())
        validate_tool = next((t for t in tools if t.name == "validate_form"), None)
        self.assertIsNotNone(validate_tool)
        assert validate_tool is not None
        props = validate_tool.inputSchema.get("properties", {})
        self.assertIn("expected_values_json", props)
        self.assertIn("alias_map_json", props)
        self.assertIn("session_id", props)


if __name__ == "__main__":
    unittest.main()
