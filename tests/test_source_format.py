import unittest
from collector_01.adapter import decode
from collector_01.sources import FixtureTransport
from pathlib import Path


class CatalogFormatTests(unittest.TestCase):
    def test_attribute_entity_and_cursor(self):
        values = decode(b'<article data-id="A&amp;B" data-title="X"></article><a rel="next" href="/p?cursor=2&amp;x=3">next</a>')
        self.assertEqual(values[0]["id"], "A&B")
        self.assertEqual(values[1]["next"], "/p?cursor=2&x=3")

    def test_throttled_page_recovers_in_same_transport(self):
        transport = FixtureTransport(Path(__file__).resolve().parents[1] / "fixtures")
        self.assertEqual(transport.request("/industry/2").status, 429)
        response = transport.request("/industry/2")
        self.assertEqual(decode(response.body)[0]["status"], "cancelled")
