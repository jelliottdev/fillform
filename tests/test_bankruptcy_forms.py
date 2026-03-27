import unittest
from pathlib import Path

from fillform.bankruptcy_forms import USCourtsBankruptcyFormsSync
from fillform.bankruptcy_tool import BankruptcySyncRequest


class BankruptcyFormsSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.syncer = USCourtsBankruptcyFormsSync(min_request_interval_seconds=0)

    def test_extract_form_pages_filters_non_form_links(self) -> None:
        html = """
        <html><body>
          <a href="/forms-rules/forms/chapter-13-plan">Form A</a>
          <a href="https://www.uscourts.gov/forms-rules/forms/voluntary-petition-individuals-filing-bankruptcy">Form B</a>
          <a href="/forms-rules/forms/bankruptcy-forms">Index</a>
          <a href="https://example.com/forms-rules/forms/chapter-7">External</a>
        </body></html>
        """
        pages = self.syncer._extract_form_pages(html)
        self.assertEqual(
            pages,
            [
                "https://www.uscourts.gov/forms-rules/forms/chapter-13-plan",
                "https://www.uscourts.gov/forms-rules/forms/voluntary-petition-individuals-filing-bankruptcy",
            ],
        )

    def test_extract_pdf_links_only_uscourts_pdf(self) -> None:
        html = """
        <html><body>
          <a href="/sites/default/files/b_101.pdf">PDF A</a>
          <a href="https://www.uscourts.gov/sites/default/files/b_122a_1.pdf?download=1">PDF B</a>
          <a href="/sites/default/files/readme.txt">TXT</a>
          <a href="https://evil.example/x.pdf">External</a>
        </body></html>
        """
        links = self.syncer._extract_pdf_links(
            "https://www.uscourts.gov/forms-rules/forms/chapter-13-plan",
            html,
        )
        self.assertEqual(
            links,
            [
                "https://www.uscourts.gov/sites/default/files/b_101.pdf",
                "https://www.uscourts.gov/sites/default/files/b_122a_1.pdf?download=1",
            ],
        )

    def test_manifest_diff_tracks_added_removed_and_changed(self) -> None:
        old = {
            "form-a": {"pdf_url": "https://www.uscourts.gov/a.pdf", "sha256": "111", "pdf_etag": "A"},
            "form-b": {"pdf_url": "https://www.uscourts.gov/b.pdf", "sha256": "222", "pdf_etag": "B"},
        }
        new = {
            "form-b": {"pdf_url": "https://www.uscourts.gov/b.pdf", "sha256": "999", "pdf_etag": "B"},
            "form-c": {"pdf_url": "https://www.uscourts.gov/c.pdf", "sha256": "333", "pdf_etag": "C"},
        }
        added, removed, changed = self.syncer._manifest_diff(old, new)
        self.assertEqual(added, ["form-c"])
        self.assertEqual(removed, ["form-a"])
        self.assertEqual(changed, ["form-b"])

    def test_manifest_diff_uses_etag_when_sha_missing(self) -> None:
        old = {"form-x": {"pdf_url": "https://www.uscourts.gov/x.pdf", "sha256": "", "pdf_etag": "W/old"}}
        new = {"form-x": {"pdf_url": "https://www.uscourts.gov/x.pdf", "sha256": "", "pdf_etag": "W/new"}}
        _added, _removed, changed = self.syncer._manifest_diff(old, new)
        self.assertEqual(changed, ["form-x"])

    def test_extract_sitemap_pages(self) -> None:
        xml = """
        <sitemapindex>
          <sitemap><loc>https://www.uscourts.gov/sitemap.xml?page=1</loc></sitemap>
          <sitemap><loc>https://www.uscourts.gov/sitemap.xml?page=2</loc></sitemap>
          <sitemap><loc>https://example.com/sitemap.xml</loc></sitemap>
        </sitemapindex>
        """
        pages = self.syncer._extract_sitemap_pages(xml)
        self.assertEqual(
            pages,
            [
                "https://www.uscourts.gov/sitemap.xml?page=1",
                "https://www.uscourts.gov/sitemap.xml?page=2",
            ],
        )

    def test_extract_sitemap_entries(self) -> None:
        xml = """
        <urlset>
          <url>
            <loc>https://www.uscourts.gov/forms-rules/forms/chapter-13-plan</loc>
            <lastmod>2026-01-31</lastmod>
          </url>
          <url>
            <loc>https://www.uscourts.gov/forms-rules/forms/chapter-11-discharge</loc>
            <lastmod>2025-12-01T10:15:30+0000</lastmod>
          </url>
        </urlset>
        """
        entries = self.syncer._extract_sitemap_entries(xml)
        self.assertEqual(
            entries,
            [
                ("https://www.uscourts.gov/forms-rules/forms/chapter-13-plan", "2026-01-31T00:00:00"),
                ("https://www.uscourts.gov/forms-rules/forms/chapter-11-discharge", "2025-12-01T10:15:30+00:00"),
            ],
        )

    def test_extract_page_metadata(self) -> None:
        html = """
        <div>Form Number: <strong>B 122A-2</strong></div>
        <div>Category: <span>Means Test Forms</span></div>
        <div>Updated on April 1, 2025</div>
        <div>Effective on April 1, 2022</div>
        """
        meta = self.syncer._extract_page_metadata(html)
        self.assertEqual(meta["form_number"], "B 122A-2")
        self.assertEqual(meta["category"], "Means Test Forms")
        self.assertEqual(meta["updated_on"], "April 1, 2025")
        self.assertEqual(meta["effective_on"], "April 1, 2022")

    def test_parse_robots_crawl_delay(self) -> None:
        robots_txt = """
        User-agent: *
        Disallow: /search
        Crawl-delay: 7
        """
        delay = self.syncer._parse_robots_crawl_delay(robots_txt)
        self.assertEqual(delay, 7.0)

    def test_parse_robots_crawl_delay_with_multiple_user_agents(self) -> None:
        robots_txt = """
        User-agent: FillForm-Bankruptcy-Sync/1.0 (+https://example.invalid/contact)
        User-agent: *
        Crawl-delay: 4
        """
        delay = self.syncer._parse_robots_crawl_delay(robots_txt)
        self.assertEqual(delay, 4.0)

    def test_bot_challenge_detection(self) -> None:
        self.assertTrue(self.syncer._looks_like_bot_challenge("Attention Required! Please verify you are human"))
        self.assertFalse(self.syncer._looks_like_bot_challenge("Official US Courts bankruptcy forms listing page"))

    def test_sync_request_validation(self) -> None:
        req = BankruptcySyncRequest.from_payload(
            {"min_request_interval_seconds": 1.5, "max_form_pages": 10},
            default_output_dir=self._tmp_path(),
            default_state_path=self._tmp_path() / "state.json",
        )
        self.assertEqual(req.min_request_interval_seconds, 1.5)
        self.assertEqual(req.max_form_pages, 10)

        with self.assertRaises(ValueError):
            BankruptcySyncRequest.from_payload(
                {"min_request_interval_seconds": 0},
                default_output_dir=self._tmp_path(),
                default_state_path=self._tmp_path() / "state.json",
            )

    def test_document_key_and_prior_lookup(self) -> None:
        key = self.syncer._document_key(
            "schedule-a-b",
            "https://www.uscourts.gov/sites/default/files/b_106a-b.pdf?download=1",
            1,
        )
        self.assertEqual(key, "schedule-a-b--b-106a-b")

        manifest = {
            "schedule-a-b--b-106a-b": {
                "slug": "schedule-a-b--b-106a-b",
                "page_url": "https://www.uscourts.gov/forms-rules/forms/schedule-a-b-property",
                "pdf_url": "https://www.uscourts.gov/sites/default/files/b_106a-b.pdf",
            },
            "schedule-a-b--instructions": {
                "slug": "schedule-a-b--instructions",
                "page_url": "https://www.uscourts.gov/forms-rules/forms/schedule-a-b-property",
                "pdf_url": "https://www.uscourts.gov/sites/default/files/b_106a-b_ins.pdf",
            },
        }
        entries = self.syncer._prior_entries_for_page(
            manifest,
            "https://www.uscourts.gov/forms-rules/forms/schedule-a-b-property",
        )
        self.assertEqual(len(entries), 2)
        one = self.syncer._prior_entry_for_pdf(
            manifest,
            "https://www.uscourts.gov/forms-rules/forms/schedule-a-b-property",
            "https://www.uscourts.gov/sites/default/files/b_106a-b_ins.pdf",
        )
        self.assertIsNotNone(one)

    def _tmp_path(self):
        # stable path helper for payload parsing tests (filesystem is not touched).
        return Path("/tmp/fillform_test")


if __name__ == "__main__":
    unittest.main()
