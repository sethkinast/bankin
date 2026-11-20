"""BanKin v1 + looks round 1 tests, including the pinned cases."""

import io
import os
import re
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

    def _challenge(self, page, kind):
        """Parse the server-rendered math challenge for one form."""
        m = re.search(
            r'action="/kid/\d+/' + kind + r'".*?name="math_token" value="([^"]+)".*?'
            r'Parent check:\s*(\d+)\s*([+-])\s*(\d+)',
            page, re.S)
        self.assertIsNotNone(m, f"no math challenge found for {kind}")
        token, a, op, b = m.groups()
        answer = int(a) + int(b) if op == "+" else int(a) - int(b)
        return {"math_token": token, "math_answer": str(answer)}

    def _move(self, kid_id, kind, amount, source="", from_home=False):
        page = self.client.get("/" if from_home else f"/kid/{kid_id}").get_data(as_text=True)
        data = {"amount": amount, "source": source, "note": ""}
        data.update(self._challenge(page, kind))
        if from_home:
            data["from_home"] = "1"
        return self.client.post(f"/kid/{kid_id}/{kind}", data=data)

    def _deposit(self, kid_id, amount, source="", from_home=False):
        return self._move(kid_id, "deposit", amount, source, from_home)

    def _withdraw(self, kid_id, amount, source="", from_home=False):
        return self._move(kid_id, "withdraw", amount, source, from_home)

    def test_pinned_cases_1_to_3(self):
        """1) deposit $20 Birthday -> $20. 2) withdraw $5 -> $15.
        3) goal 'Lego set' $60 -> 25% at $15."""
        kid_id = self._add_kid()
        self._deposit(kid_id, "20.00", "Birthday")
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$20.00", page)
        self._withdraw(kid_id, "5.00")
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$15.00", page)
        self.client.post(f"/kid/{kid_id}/goal",
                         data={"goal_name": "Lego set", "goal_price": "60.00"})
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("Lego set", page)
        self.assertIn("25%", page)

    def test_pinned_case_4_no_goal_renders_cleanly(self):
        kid_id = self._add_kid()
        self._deposit(kid_id, "10.00", "Birthday")
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
        self._deposit(kid_id, "60.00", "Birthday")
        self.client.post(f"/kid/{kid_id}/goal",
                         data={"goal_name": "Lego set", "goal_price": "60.00"})
        self.client.post(f"/kid/{kid_id}/goal/complete")
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$0.00", page)
        self.assertIn("Completed goals", page)
        self.assertIn("No active goal", page)

    def test_withdrawal_beyond_balance_is_rejected(self):
        kid_id = self._add_kid()
        self._deposit(kid_id, "5.00")
        self._withdraw(kid_id, "10.00")
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$5.00", page)


class TestLooksRound1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _add_kid(self, name="Test Kid"):
        resp = self.client.post("/kids", data={"name": name}, follow_redirects=False)
        kid_id = int(resp.headers["Location"].rsplit("/", 1)[-1])
        return kid_id

    def _challenge(self, page, kind):
        m = re.search(
            r'action="/kid/\d+/' + kind + r'".*?name="math_token" value="([^"]+)".*?'
            r'Parent check:\s*(\d+)\s*([+-])\s*(\d+)',
            page, re.S)
        self.assertIsNotNone(m, f"no math challenge found for {kind}")
        token, a, op, b = m.groups()
        answer = int(a) + int(b) if op == "+" else int(a) - int(b)
        return {"math_token": token, "math_answer": str(answer)}

    def test_home_is_logo_then_themed_cards(self):
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/theme", data={"theme": "mint"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Ban", page)
        self.assertIn("Test Kid", page)
        # Mint theme: pastel bg, matching border, matching balance colour.
        self.assertIn("background:#dff5e6", page)
        self.assertIn("border-color:#7fc9a0", page)
        self.assertIn("color:#14663a", page)
        # Whole-card tap target + flyout bubbles, no prominent Add Kid button.
        self.assertIn("kid-card", page)
        self.assertIn("flyout-", page)
        self.assertIn("Deposit", page)
        self.assertIn("Withdraw", page)
        self.assertIn('class="add-kid"', page)

    def test_home_card_shows_goal_with_separator_only_when_goal(self):
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/deposit",
                         data={"amount": "15.00", "source": "",
                               "note": "", **self._challenge(
                                   self.client.get(f"/kid/{kid_id}").get_data(as_text=True),
                                   "deposit")})
        self.client.post(f"/kid/{kid_id}/goal",
                         data={"goal_name": "Lego set", "goal_price": "60.00"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('<hr class="goal-sep">', page)
        self.assertIn("Lego set", page)
        self.assertIn("25%", page)

    def test_home_card_without_goal_has_no_goal_section(self):
        self._add_kid()
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('<hr class="goal-sep">', page)

    def test_avatar_upload_and_fallback_initials(self):
        kid_id = self._add_kid("Wren Kinast")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("WK", page)  # initials fallback
        img = Image.new("RGB", (10, 10), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        resp = self.client.post(
            f"/kid/{kid_id}/avatar",
            data={"avatar": (io.BytesIO(buf.getvalue()), "pic.png")},
            content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 302)
        resp = self.client.get(f"/avatar/{kid_id}")
        self.assertEqual(resp.status_code, 200)
        resp.close()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(f"/avatar/{kid_id}", page)

    def test_math_gate_wrong_answer_records_nothing(self):
        kid_id = self._add_kid()
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        fields = self._challenge(page, "deposit")
        fields["math_answer"] = str(int(fields["math_answer"]) + 1)
        self.client.post(f"/kid/{kid_id}/deposit",
                         data={"amount": "20.00", "source": "Birthday",
                               "note": "", **fields})
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$0.00", page)
        self.assertIn("math answer", page)

    def test_math_gate_missing_token_records_nothing(self):
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/deposit",
                         data={"amount": "20.00", "source": "Birthday", "note": ""})
        page = self.client.get(f"/kid/{kid_id}").get_data(as_text=True)
        self.assertIn("$0.00", page)

    def test_math_gate_correct_answer_via_home_popover(self):
        kid_id = self._add_kid()
        page = self.client.get("/").get_data(as_text=True)
        fields = self._challenge(page, "deposit")
        resp = self.client.post(
            f"/kid/{kid_id}/deposit",
            data={"amount": "20.00", "source": "Birthday", "note": "",
                  "from_home": "1", **fields}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["Location"], "/")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("$20.00", page)
        # One-time token: replaying the same challenge is rejected.
        resp = self.client.post(
            f"/kid/{kid_id}/deposit",
            data={"amount": "20.00", "source": "Birthday", "note": "",
                  "from_home": "1", **fields})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("$20.00", page)  # still $20, not $40

    def test_unknown_theme_is_rejected(self):
        kid_id = self._add_kid()
        self.client.post(f"/kid/{kid_id}/theme", data={"theme": "nope"})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("background:#e3f0ff", page)  # still default sky


if __name__ == "__main__":
    unittest.main()
