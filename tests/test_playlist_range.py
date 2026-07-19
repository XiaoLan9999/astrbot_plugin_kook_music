import unittest

from playlist_range import validate_playlist_range


class PlaylistRangeValidationTests(unittest.TestCase):
    def test_accepts_inclusive_range_at_capacity(self):
        result, error = validate_playlist_range("201-400", 1267, 200)
        self.assertEqual(result, (201, 400))
        self.assertIsNone(error)

    def test_accepts_supported_separators_and_spaces(self):
        result, error = validate_playlist_range(" 1 至 25 ", 100, 25)
        self.assertEqual(result, (1, 25))
        self.assertIsNone(error)

    def test_rejects_range_larger_than_available_queue(self):
        result, error = validate_playlist_range("1-200", 1267, 180)
        self.assertIsNone(result)
        self.assertIn("180", error)

    def test_rejects_out_of_bounds_or_reversed_range(self):
        for text in ("0-10", "20-10", "1200-1300"):
            with self.subTest(text=text):
                result, error = validate_playlist_range(text, 1267, 200)
                self.assertIsNone(result)
                self.assertIn("1-1267", error)

    def test_rejects_invalid_format(self):
        result, error = validate_playlist_range("200", 1267, 200)
        self.assertIsNone(result)
        self.assertIn("格式错误", error)


if __name__ == "__main__":
    unittest.main()
