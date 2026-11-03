"""BanKin v1 tests, including the five pinned cases from the brief."""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from app import (  # noqa: E402
    create_app,
    format_cents,
    goal_progress_pct,
    parse_amount_to_cents,
    push_to_tv,
    render_tv_card,
)


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"OK"


class TestMoney(unittest.TestCase):
    def test_parse_amounts(self):
        self.assertEqual(parse_amount_to_cents("20"), 2000)
        self.assertEqual(parse_amount_to_cents("20.00"), 2000)
        self.assertEqual(parse_amount_to_cents("$5.50"), 550)

    def test_parse_rejects_bad_amounts(self):
        for bad in ("", "abc", "-5", "0", "1.234"):
            with self.assertRaises(ValueError, msg=bad):
                parse_amount_to_cents(bad)

    def test_format_cents(self):
        self.assertEqual(format_cents(2000), "$20.00")
        self.assertEqual(format_cents(1500), "$15.00")


class TestGoalProgress(unittest.TestCase):
    def test_quarter_progress(self):
        self.assertEqual(goal_progress_pct(1500, 6000), 25)

    def test_capped_at_100(self):
        self.assertEqual(goal_progress_pct(9999, 6000), 100)

    def test_no_price_is_zero(self):
        self.assertEqual(goal_progress_pct(1500, 0), 0)


class TestTvCard(unittest.TestCase):
    def test_card_is_exactly_240x240_jpeg(self):
        data = render_tv_card("Test Kid", 1500, "Lego set", 6000)
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.size, (240, 240))
        self.assertEqual(img.format, "JPEG")

    def test_card_without_goal(self):
        data = render_tv_card("Test Kid", 1500)
        img = Image.open(io.BytesIO(data))
        self.assertEqual(img.size, (240, 240))
        self.assertEqual(img.format, "JPEG")


class TestPush(unittest.TestCase):
    def test_push_sequence_and_filename(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            if isinstance(req, str):
                calls.append(("GET", req, None))
            else:
                calls.append((req.get_method(), req.full_url, req.data))
            return FakeResponse()

        ok, _msg = push_to_tv("192.168.2.50", 7, b"fake-jpeg", urlopen=fake_urlopen)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        method, url, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "http://192.168.2.50/doUpload?dir=/image/")
        self.assertIn(b'filename="bankin-7.jpg"', body)
        self.assertIn(b'name="file"', body)
        self.assertEqual(calls[1],
                         ("GET", "http://192.168.2.50/set?img=/image/bankin-7.jpg", None))
        self.assertEqual(calls[2],
                         ("GET", "http://192.168.2.50/set?theme=3", None))

    def test_push_failure_is_reported_not_raised(self):
        def failing(_req, timeout=None):
            raise OSError("connection refused")

        ok, msg = push_to_tv("192.168.2.50", 7, b"x", urlopen=failing)
        self.assertFalse(ok)
        self.assertIn("Ledger was not changed", msg)


class TestAppPinnedCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _add_kid(self, name="Test Kid"):
        resp = self.client.post("/kids", data={"name": name}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        kid_id = int(resp.headers["Location"].rsplit("/", 1)[-1])
        return kid_id

    def test_pinned_cases_1_to_3(self):
        """1) deposit $20 Birthday -> $20. 2) withdraw $5 -> $15.
        3) goal 'Lego set' $60 -> 25% at $15."""
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/deposit",
                         data={"amount": "20.00", "source": "Birthday", "note": ""})
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$20.00", page)
        self.client.post(f"/kid/{kid_id}/withdraw",
                         data={"amount": "5.00", "source": "", "note": ""})
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$15.00", page)
        self.client.post(f"/kid/{kid_id}/goal",
                         data={"goal_name": "Lego set", "goal_price": "60.00"})
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("Lego set", page)
        self.assertIn("25%", page)

    def test_pinned_case_4_no_goal_renders_cleanly(self):
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/deposit",
                         data={"amount": "10.00", "source": "Birthday", "note": ""})
        resp = self.client.get(f"/kid/{kid_id}")
        self.assertEqual(resp.status_code, 200)
        page = resp.get_data(as_text=True)
        self.assertIn("$10.00", page)
        self.assertIn("No active goal", page)
        self.assertNotIn("of $", page.split("No active goal")[0])

    def test_pinned_case_5_tv_endpoint_size(self):
        kid_id = self._add_kid()
        resp = self.client.get(f"/kid/{kid_id}/tv.jpg")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/jpeg")
        img = Image.open(io.BytesIO(resp.data))
        self.assertEqual(img.size, (240, 240))

    def test_goal_complete_records_withdrawal_and_archives(self):
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/deposit",
                         data={"amount": "60.00", "source": "Birthday", "note": ""})
        self.client.post(f"/kid/{kid_id}/goal",
                         data={"goal_name": "Lego set", "goal_price": "60.00"})
        self.client.post(f"/kid/{kid_id}/goal/complete")
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$0.00", page)
        self.assertIn("Completed goals", page)
        self.assertIn("No active goal", page)

    def test_withdrawal_beyond_balance_is_rejected(self):
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/deposit",
                         data={"amount": "5.00", "source": "", "note": ""})
        self.client.post(f"/kid/{kid_id}/withdraw",
                         data={"amount": "10.00", "source": "", "note": ""})
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$5.00", page)


if __name__ == "__main__":
    unittest.main()
