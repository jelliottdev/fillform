import base64
import tempfile
import unittest
from pathlib import Path

from fillform import mcp_support


class McpSupportTests(unittest.TestCase):
    def test_normalize_pdf_path_supports_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

            sandbox_path = mcp_support._normalize_pdf_path(f"sandbox:{pdf_path}")
            file_url_path = mcp_support._normalize_pdf_path(f"file://{pdf_path}")

            self.assertEqual(sandbox_path, pdf_path.resolve())
            self.assertEqual(file_url_path, pdf_path.resolve())

    def test_resolve_pdf_source_from_base64(self) -> None:
        payload = base64.b64encode(b"%PDF-1.4\n%%EOF\n").decode()
        resolved = mcp_support.resolve_pdf_source({"pdf_bytes_base64": payload})
        self.assertTrue(resolved.exists())
        self.assertEqual(resolved.read_bytes(), b"%PDF-1.4\n%%EOF\n")

    def test_session_stores_fingerprint(self) -> None:
        sid = mcp_support.create_session(
            pdf_path=Path("/tmp/test.pdf"),
            alias_map={"F001": "Debtor 1"},
            pdf_fingerprint="abc123",
        )
        sess = mcp_support.get_session(sid)
        self.assertIsNotNone(sess)
        assert sess is not None
        self.assertEqual(sess["pdf_fingerprint"], "abc123")

    def test_compute_pdf_fingerprint_stable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf_path = Path(td) / "sample.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
            fp1 = mcp_support.compute_pdf_fingerprint(pdf_path)
            fp2 = mcp_support.compute_pdf_fingerprint(pdf_path)
            self.assertEqual(fp1, fp2)
            self.assertEqual(len(fp1), 64)


if __name__ == "__main__":
    unittest.main()
