import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music_auth.bilibili_cookie import (
    COOKIE_NAMES,
    BilibiliCookieInputError,
    parse_bilibili_cookie,
)

NOW = 2_000_000_000
FUTURE = NOW + 1000
SESSION = "synthetic%2Csession%2Btoken%3D"


def cookie(name="SESSDATA", value=SESSION, **changes):
    result = {
        "domain": ".bilibili.com",
        "hostOnly": False,
        "path": "/",
        "name": name,
        "value": value,
        "expirationDate": FUTURE,
        "session": False,
        "secure": True,
        "httpOnly": True,
    }
    result.update(changes)
    return result


def netscape(name="SESSDATA", value=SESSION, **changes):
    fields = {
        "domain": ".bilibili.com",
        "subdomains": "TRUE",
        "path": "/",
        "secure": "TRUE",
        "expires": str(FUTURE),
        "name": name,
        "value": value,
    }
    fields.update(changes)
    return "\t".join(fields.values())


class BilibiliCookieFormatTests(unittest.TestCase):
    def setUp(self):
        clock = patch(
            "astrbot_plugin_kook_music.music_auth.bilibili_cookie.time.time",
            return_value=NOW,
        )
        clock.start()
        self.addCleanup(clock.stop)

    def parsed(self, text):
        return parse_bilibili_cookie(text)["cookies"]

    def rejected(self, text, code=None):
        with self.assertRaises(BilibiliCookieInputError) as caught:
            self.parsed(text)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn(SESSION, str(caught.exception))

    def test_exports_produce_the_same_authentication_fields(self):
        expected = {"SESSDATA": SESSION, "bili_jct": "synthetic-csrf", "DedeUserID": "123"}
        entries = [cookie(name, value) for name, value in expected.items()]
        for text in (
            json.dumps(entries),
            ";".join(f"{name}={value}" for name, value in expected.items()),
            "# Netscape HTTP Cookie File\n"
            + "\n".join(netscape(name, value) for name, value in expected.items()),
        ):
            with self.subTest(text=text[:12]):
                self.assertEqual(self.parsed(text), expected)

    def test_bare_sessdata_preserves_original_percent_encoding_and_commas(self):
        self.assertEqual(self.parsed(SESSION), {"SESSDATA": SESSION})
        self.assertEqual(self.parsed("synthetic,session,token"), {"SESSDATA": "synthetic,session,token"})

    def test_cookie_prefix_and_equals_in_values(self):
        self.assertEqual(
            self.parsed(" COOKIE: SESSDATA = synthetic== ; bili_jct = csrf "),
            {"SESSDATA": "synthetic==", "bili_jct": "csrf"},
        )

    def test_only_authentication_and_device_whitelist_is_retained(self):
        text = ";".join(f"{name}=synthetic" for name in COOKIE_NAMES)
        text += ";refresh_token=do-not-send;sid=analytics;other=abc,123;empty="
        self.assertEqual(set(self.parsed(text)), COOKIE_NAMES)

    def test_json_and_netscape_filter_unknown_fields(self):
        self.assertEqual(self.parsed(json.dumps([cookie(), cookie("tracking", "a,b")])), {"SESSDATA": SESSION})
        self.assertEqual(self.parsed(netscape() + "\n" + netscape("refresh_token", "secret")), {"SESSDATA": SESSION})

    def test_bom_and_fences_for_all_formats(self):
        for text in (
            "\ufeff" + SESSION,
            "```\nSESSDATA=" + SESSION + "\n```",
            "```json\n" + json.dumps([cookie()]) + "\n```",
            "```netscape\n# Netscape HTTP Cookie File\n" + netscape() + "\n```",
        ):
            self.assertEqual(self.parsed(text), {"SESSDATA": SESSION})

    def test_malformed_fences_and_non_string_inputs_are_rejected(self):
        for text in ("```json\n[]", "```python\n" + SESSION + "\n```", None, 1, b"SESSDATA=x"):
            self.rejected(text)

    def test_known_names_can_have_markdown_escapes_but_values_cannot(self):
        self.assertEqual(self.parsed(r"SESSDATA=synthetic;bili\_jct=csrf"), {"SESSDATA": "synthetic", "bili_jct": "csrf"})
        self.assertEqual(self.parsed(json.dumps([cookie(), cookie("bili_jct", "csrf")]).replace('"bili_jct"', r'"bili\_jct"')), {"SESSDATA": SESSION, "bili_jct": "csrf"})
        self.rejected(json.dumps([cookie(value=r"synthetic\_session")]), "COOKIE_VALUE")

    def test_header_control_characters_and_request_injection_are_rejected(self):
        for text in (
            "SESSDATA=" + SESSION + "\r\nX-Other: value",
            "GET / HTTP/1.1\nCookie: SESSDATA=" + SESSION,
            "SESSDATA=" + SESSION + "\0",
            "SESSDATA=" + SESSION + "\x7f",
            "SESSDATA=synthetic\tsession",
        ):
            self.rejected(text)

    def test_json_embedded_controls_and_invalid_token_chars_are_rejected(self):
        for value in ("synthetic\r\nsession", "synthetic\tsession", '"quoted"', "synthetic;session", "synthetic\\session", "synthetic session", "\u4e2d\u6587"):
            self.rejected(json.dumps([cookie(value=value)]), "COOKIE_VALUE")

    def test_discarded_fields_cannot_hide_controls_or_malformed_names(self):
        self.rejected(json.dumps([cookie(), cookie("tracking", "value\r\nX-Other: x")]), "COOKIE_VALUE")
        self.rejected(json.dumps([cookie(), cookie("tracking\r\nX-Other", "value")]))
        self.rejected("SESSDATA=" + SESSION + ";Set-Cookie: other=value")

    def test_duplicate_identical_values_merge_in_all_formats(self):
        for text in (
            f"SESSDATA={SESSION};SESSDATA={SESSION}",
            json.dumps([cookie(), cookie()]),
            netscape() + "\n" + netscape(),
        ):
            self.assertEqual(self.parsed(text), {"SESSDATA": SESSION})

    def test_conflicts_are_rejected_even_across_official_scopes(self):
        for text in (
            f"SESSDATA={SESSION};SESSDATA=different",
            json.dumps([cookie(), cookie(value="different", domain="www.bilibili.com", hostOnly=True)]),
            netscape() + "\n" + netscape(value="different", domain="api.bilibili.com", subdomains="FALSE"),
        ):
            self.rejected(text, "COOKIE_CONFLICT")

    def test_official_well_formed_subdomains_are_accepted(self):
        for domain, host_only in (("bilibili.com", True), (".bilibili.com", False), ("www.bilibili.com", True), ("api.bilibili.com", True), ("passport.bilibili.com", False), (".sub.bilibili.com", False)):
            self.assertEqual(self.parsed(json.dumps([cookie(domain=domain, hostOnly=host_only)])), {"SESSDATA": SESSION})

    def test_wrong_or_malformed_domains_are_rejected(self):
        for domain in ("bilibili.com.evil.example", "notbilibili.com", "evil.example", ".com", "bilibili.com.", "https://www.bilibili.com", "bilibili.com:443", "..bilibili.com", "_bad.bilibili.com", "-bad.bilibili.com", "bad-.bilibili.com", "a" * 64 + ".bilibili.com"):
            self.rejected(json.dumps([cookie(domain=domain)]), "COOKIE_SCOPE")

    def test_other_domain_entries_cannot_be_hidden_as_analytics(self):
        self.rejected(json.dumps([cookie(), cookie("other", "value", domain="evil.example")]), "COOKIE_SCOPE")

    def test_only_root_path_can_be_flattened_for_multiple_apis(self):
        for path in ("", "/x", "/x/", "/account", "https://api.bilibili.com/", None):
            self.rejected(json.dumps([cookie(path=path)]), "COOKIE_SCOPE")

    def test_inconsistent_host_only_and_invalid_metadata_are_rejected(self):
        self.rejected(json.dumps([cookie(hostOnly=True)]), "COOKIE_SCOPE")
        for field in ("hostOnly", "session", "secure", "httpOnly"):
            self.rejected(json.dumps([cookie(**{field: "false"})]))

    def test_expired_optional_values_are_filtered_and_expired_session_is_rejected(self):
        self.assertEqual(self.parsed(json.dumps([cookie(), cookie("bili_jct", "expired", expirationDate=NOW)])), {"SESSDATA": SESSION})
        self.rejected(json.dumps([cookie(expirationDate=NOW)]), "COOKIE_SESSDATA")
        self.rejected(netscape(expires=str(NOW - 1)), "COOKIE_SESSDATA")

    def test_expired_conflict_does_not_replace_valid_session(self):
        self.assertEqual(self.parsed(json.dumps([cookie(), cookie(value="expired", expirationDate=NOW)])), {"SESSDATA": SESSION})

    def test_null_and_session_expiry_are_supported(self):
        self.assertEqual(self.parsed(json.dumps([cookie(expirationDate=None, session=True)])), {"SESSDATA": SESSION})
        self.assertEqual(self.parsed(netscape(expires="0")), {"SESSDATA": SESSION})

    def test_expiry_must_be_a_finite_nonnegative_number(self):
        for value in (True, "forever", -1, float("inf"), float("nan"), 10**500):
            self.rejected(json.dumps([cookie(expirationDate=value)]))

    def test_invalid_json_shapes_and_duplicate_object_keys_are_rejected(self):
        for text in ("{}", "[]", "[null]", "[[]]", '[{"name":"SESSDATA","name":"bili_jct"}]', '{"SESSDATA":"synthetic"}', json.dumps([cookie(value=123)]), json.dumps([cookie(name=None)])):
            self.rejected(text)

    def test_netscape_http_only_comments_crlf_and_escaped_names(self):
        text = "# Netscape HTTP Cookie File\r\n# exported\r\n#HttpOnly_" + netscape() + "\r\n" + netscape(name=r"bili\_jct", value="csrf")
        self.assertEqual(self.parsed(text), {"SESSDATA": SESSION, "bili_jct": "csrf"})
        self.assertEqual(self.parsed("\\#HttpOnly_" + netscape()), {"SESSDATA": SESSION})

    def test_netscape_malformed_columns_flags_and_expiry_are_rejected(self):
        for text in (netscape().replace("\t", " "), netscape() + "\textra", netscape(secure="maybe"), netscape(subdomains="0"), netscape(expires="NaN")):
            self.rejected("# Netscape HTTP Cookie File\n" + text)

    def test_no_optional_field_can_replace_sessdata(self):
        for text in ("bili_jct=csrf", "SESSDATA=", json.dumps([cookie("buvid3", "device")])):
            self.rejected(text, "COOKIE_SESSDATA")

    def test_input_limits_are_bounded_by_utf8_bytes(self):
        self.rejected("SESSDATA=" + SESSION + ";other=" + "x" * 65536, "COOKIE_SIZE")
        self.rejected("SESSDATA=" + SESSION + ";other=" + "\u4e2d" * 22000, "COOKIE_SIZE")
        self.rejected("SESSDATA=" + "x" * 8192, "COOKIE_SIZE")
        self.assertEqual(self.parsed("SESSDATA=" + SESSION + ";other=" + "x" * 9000), {"SESSDATA": SESSION})

    def test_export_entry_count_is_bounded(self):
        for text in (json.dumps([cookie()] * 257), "\n".join(netscape() for _ in range(257)), "SESSDATA=" + SESSION + ";" + ";".join("other=x" for _ in range(256))):
            self.rejected(text, "COOKIE_SIZE")


if __name__ == "__main__":
    unittest.main()
