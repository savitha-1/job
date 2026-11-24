import tempfile
import unittest
import zipfile
from pathlib import Path

from application_planner import plan
from candidate_profile import extract_text, suggest_facts
from discovery_state import DiscoveryStore
from ledger import Ledger


class LocalFeatureTests(unittest.TestCase):
    def test_frontier_priority_host_cap_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            store = DiscoveryStore(Path(d) / "state.db")
            store.enqueue("https://a.example/low", 1)
            store.enqueue("https://a.example/high", 9)
            store.enqueue("https://b.example/job", 5)
            self.assertEqual(store.take(limit=5, per_host=1), ["https://a.example/high", "https://b.example/job"])
            store.checkpoint("crawler", {"offset": 12})
            self.assertEqual(store.load_checkpoint("crawler"), {"offset": 12})
            with self.assertRaises(ValueError):
                store.enqueue("http://a.example/unsafe")

    def test_employer_idempotency_and_job_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            store = DiscoveryStore(Path(d) / "state.db")
            self.assertEqual(store.add_employer("https://jobs.example/careers"), "jobs.example")
            self.assertEqual(store.add_employer("jobs.example"), "jobs.example")
            self.assertTrue(store.save_job("jobs.example", {"source": "x", "board": "b", "id": "1"}))
            key = "x\x1fb\x1f1"
            self.assertEqual(len(store.job_history(key)), 1)
            store.save_job("jobs.example", {"source": "x", "board": "b", "id": "1", "title": "Updated title"})
            self.assertEqual(len(store.job_history(key)), 2)
            with self.assertRaises(ValueError):
                store.add_evidence("jobs.example", "file:///etc/passwd", "test")

    def test_local_text_resume_suggestions_require_approval(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "resume.txt"
            p.write_text("Contact me at engineer@example.com\nhttps://github.com/sample", encoding="utf-8")
            text = extract_text(p)
            facts = suggest_facts(text, str(p))
            self.assertEqual(facts["email"]["value"], "engineer@example.com")
            self.assertFalse(facts["email"]["approved"])

    def test_docx_resume_text_is_extracted_locally(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "resume.docx"
            xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>ada@example.com</w:t></w:r></w:p></w:body></w:document>'
            with zipfile.ZipFile(p, "w") as archive:
                archive.writestr("word/document.xml", xml)
            self.assertIn("ada@example.com", extract_text(p))

    def test_pdf_resume_text_is_extracted_locally(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "resume.pdf"
            writer = PdfWriter()
            page = writer.add_blank_page(width=300, height=300)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
            page[NameObject("/Resources")] = writer._add_object(DictionaryObject({NameObject("/Font"): writer._add_object(fonts)}))
            stream = DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 30 250 Td (candidate@example.com) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
            with p.open("wb") as output:
                writer.write(output)
            self.assertIn("candidate@example.com", extract_text(p))

    def test_application_planner_never_authorizes_submission(self):
        result = plan([{"id": "name", "label": "First name", "required": True, "type": "text"}], {"first_name": "Ada"})
        self.assertFalse(result["submission_authorized"])
        self.assertEqual(result["actions"][0]["profile_key"], "first_name")

    def test_ledger_enforces_one_open_opportunity_per_employer_per_batch(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / "apps.sqlite")
            self.assertTrue(ledger.enqueue("https://jobs.example/role/1", "Example Inc", "2026-11-24"))
            self.assertFalse(ledger.enqueue("https://jobs.example/role/2", "example inc", "2026-11-24"))
            self.assertTrue(ledger.enqueue("https://jobs.example/role/2", "EXAMPLE INC", "2026-11-25"))
            self.assertFalse(ledger.enqueue("https://jobs.example/role/1", "EXAMPLE INC", "2026-11-25"))


if __name__ == "__main__":
    unittest.main()
