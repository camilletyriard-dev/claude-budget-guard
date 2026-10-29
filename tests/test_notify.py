import json
import os
import unittest
from unittest import mock

from claude_budget.notify import level, money, overview, slack


class NotifyTest(unittest.TestCase):
    def test_slack_without_url_is_a_no_op(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("urllib.request.urlopen") as post:
            self.assertFalse(slack("hi"))
        post.assert_not_called()

    def test_slack_posts_json(self):
        with mock.patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"}), \
                mock.patch("urllib.request.urlopen") as post:
            self.assertTrue(slack("hi"))
        req = post.call_args[0][0]
        self.assertEqual(req.full_url, "https://hooks.slack.com/x")
        self.assertEqual(json.loads(req.data), {"text": "hi"})

    def test_slack_failure_never_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), \
                mock.patch("sys.stderr"):
            self.assertFalse(slack("hi", url="https://hooks.slack.com/x"))

    def test_helpers(self):
        self.assertEqual(level(0.79, [0.5, 0.8]), 0.5)
        self.assertEqual(level(0.1, [0.5, 0.8]), 0)
        self.assertEqual(money(12.5), "$12.50")
        self.assertEqual(money(50, 200), "$50.00 of $200.00 (25%)")
        text = overview("T", {"A": [("x", 1, None), ("y", 3, 4)], "Empty": []}, top=1)
        self.assertEqual(text, "T\nA: *y* $3.00 of $4.00 (75%) · +1 more")


if __name__ == "__main__":
    unittest.main()
