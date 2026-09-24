import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_kook_music.music_auth.manual_cookie import (
    CookieInputError,
    parse_netease_cookie,
)

NOW = 2_000_000_000
FUTURE = NOW + 1000


def cookie(name="MUSIC_U", value="synthetic-session", **changes):
    result = {
        "name": name,
        "value": value,
        "domain": ".music.163.com",
        "hostOnly": False,
        "path": "/",
        "expirationDate": FUTURE,
    }
    result.update(changes)
    return result


def netscape(name="MUSIC_U", value="synthetic-session", **changes):
    fields = {
        "domain": ".music.163.com",
        "subdomains": "TRUE",
        "path": "/",
        "secure": "FALSE",
        "expiry": str(FUTURE),
        "name": name,
        "value": value,
    }
    fields.update(changes)
    return "\t".join(fields.values())


class CookieExportTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch(
            "astrbot_plugin_kook_music.music_auth.manual_cookie.time.time",
            return_value=NOW,
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def parsed(self, text):
        return parse_netease_cookie(text)["cookies"]

    def rejected(self, text, code=None):
        with self.assertRaises(CookieInputError) as caught:
            self.parsed(text)
        if code:
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn("synthetic-session", str(caught.exception))

    def test_three_exports_have_identical_minimal_credentials(self):
        self.assertEqual(
            self.parsed("MUSIC_U=synthetic-session;__csrf=csrf"),
            self.parsed(json.dumps([cookie(), cookie("__csrf", "csrf")])),
        )
        self.assertEqual(
            self.parsed("MUSIC_U=synthetic-session;__csrf=csrf"),
            self.parsed(netscape() + "\n" + netscape("__csrf", "csrf")),
        )

    def test_header_analytics_comma_and_empty_values_are_discarded(self):
        self.assertEqual(
            self.parsed(
                "Cookie: MUSIC_U=synthetic-session;_ntes_nnid=abc,123; NMTID=;__csrf=;other="
            ),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_only_four_authentication_fields_are_kept(self):
        self.assertEqual(
            set(
                self.parsed(
                    "MUSIC_U=synthetic-session;__csrf=csrf;MUSIC_A=guest;MUSIC_R_T=refresh;WEVNSM=1;NMTID=track"
                )
            ),
            {"MUSIC_U", "__csrf", "MUSIC_A", "MUSIC_R_T"},
        )

    def test_header_prefix_whitespace_and_equals_are_preserved(self):
        self.assertEqual(
            self.parsed(" COOKIE: MUSIC_U = synthetic== ; __csrf = csrf "),
            {"MUSIC_U": "synthetic==", "__csrf": "csrf"},
        )

    def test_known_name_markdown_escaping_does_not_change_values(self):
        self.assertEqual(
            self.parsed(r"MUSIC\_U=synthetic-session;\_\_csrf=csrf"),
            {"MUSIC_U": "synthetic-session", "__csrf": "csrf"},
        )
        self.assertEqual(
            self.parsed(json.dumps([cookie(name=r"MUSIC\_U")])),
            {"MUSIC_U": "synthetic-session"},
        )
        self.rejected(r"MUSIC_U=synthetic\_session", "COOKIE_VALUE")

    def test_chat_escaped_known_json_name_can_be_unwrapped(self):
        exported = json.dumps([cookie(), cookie("__csrf", "csrf")])
        exported = exported.replace('"MUSIC_U"', r'"MUSIC\_U"').replace(
            '"__csrf"', r'"\_\_csrf"'
        )
        self.assertEqual(
            self.parsed(exported), {"MUSIC_U": "synthetic-session", "__csrf": "csrf"}
        )

    def test_chat_json_unwrap_never_repairs_value_or_unknown_field_name(self):
        self.rejected(
            json.dumps([cookie()]).replace(
                '"synthetic-session"', r'"synthetic\_session"'
            )
        )
        self.rejected(
            json.dumps([cookie(), cookie("unknown", "value")]).replace(
                '"unknown"', r'"unknown\_field"'
            )
        )
        self.rejected(json.dumps([cookie()]).replace('"domain"', r'"do\_main"'))

    def test_bom_and_code_fences_are_supported(self):
        for text in (
            "\ufeffMUSIC_U=synthetic-session",
            "```\nMUSIC_U=synthetic-session\n```",
            "```json\n" + json.dumps([cookie()]) + "\n```",
            "\ufeff```text\n# Netscape HTTP Cookie File\n" + netscape() + "\n```",
        ):
            with self.subTest(kind=text[:10]):
                self.assertEqual(self.parsed(text), {"MUSIC_U": "synthetic-session"})

    def test_header_newline_and_request_injection_are_rejected(self):
        for text in (
            "MUSIC_U=synthetic-session\r\nX-Other=x",
            "GET / HTTP/1.1\nCookie: MUSIC_U=synthetic-session",
            "MUSIC_U=synthetic-session\0x",
            "MUSIC_U=synthetic-session\x01x",
        ):
            self.rejected(text)

    def test_header_duplicate_same_value_merges(self):
        self.assertEqual(
            self.parsed("MUSIC_U=synthetic-session;MUSIC_U=synthetic-session"),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_header_duplicate_different_value_rejects(self):
        self.rejected("MUSIC_U=synthetic-session;MUSIC_U=different", "COOKIE_CONFLICT")

    def test_json_duplicate_same_value_merges(self):
        self.assertEqual(
            self.parsed(json.dumps([cookie(), cookie()])),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_json_duplicate_different_value_rejects(self):
        self.rejected(
            json.dumps([cookie(), cookie(value="different")]), "COOKIE_CONFLICT"
        )

    def test_json_more_specific_domain_wins_in_both_orders(self):
        less = cookie(value="parent", domain=".163.com")
        more = cookie()
        for entries in ([less, more], [more, less]):
            self.assertEqual(
                self.parsed(json.dumps(entries)), {"MUSIC_U": "synthetic-session"}
            )

    def test_json_host_only_and_api_path_priority(self):
        self.assertEqual(
            self.parsed(
                json.dumps(
                    [
                        cookie(value="domain"),
                        cookie(domain="music.163.com", hostOnly=True),
                    ]
                )
            ),
            {"MUSIC_U": "synthetic-session"},
        )
        self.assertEqual(
            self.parsed(json.dumps([cookie(value="root"), cookie(path="/weapi/")])),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_json_lower_scope_conflict_is_not_hidden_by_higher_scope(self):
        self.rejected(
            json.dumps(
                [
                    cookie(),
                    cookie(value="one", domain=".163.com"),
                    cookie(value="two", domain=".163.com"),
                ]
            ),
            "COOKIE_CONFLICT",
        )

    def test_json_domain_scope_is_not_suffix_guessed(self):
        for domain in (
            "evil.example",
            "music.163.com.evil.example",
            "evil.music.163.com",
            "163.com.",
            "https://music.163.com",
            ".163.com/",
        ):
            self.rejected(json.dumps([cookie(domain=domain)]), "COOKIE_SCOPE")

    def test_json_parent_host_only_is_not_valid_for_music_host(self):
        self.rejected(
            json.dumps([cookie(domain="163.com", hostOnly=True)]), "COOKIE_SCOPE"
        )
        self.assertEqual(
            self.parsed(json.dumps([cookie(domain=".163.com")])),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_json_non_playback_paths_rejected(self):
        for path in ("/account", "/weapi/w/nuser", "/weapi2", "", None):
            self.rejected(json.dumps([cookie(path=path)]), "COOKIE_SCOPE")

    def test_json_expired_exact_cookie_falls_back_to_unexpired_parent(self):
        self.assertEqual(
            self.parsed(
                json.dumps(
                    [
                        cookie(domain=".163.com"),
                        cookie(value="expired", expirationDate=NOW),
                    ]
                )
            ),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_json_expired_only_cookie_is_rejected(self):
        self.rejected(json.dumps([cookie(expirationDate=NOW - 1)]), "COOKIE_MUSIC_U")

    def test_json_session_cookie_and_null_expiration(self):
        self.assertEqual(
            self.parsed(json.dumps([cookie(expirationDate=None, session=True)])),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_json_invalid_expiration_types_rejected(self):
        for value in ("forever", "123", True, -1, float("nan"), float("inf"), 10**500):
            self.rejected(json.dumps([cookie(expirationDate=value)]))

    def test_json_does_not_accept_non_string_fields(self):
        for field in ("name", "value", "domain", "path"):
            for value in (None, 1, True, {}, []):
                self.rejected(json.dumps([cookie(**{field: value})]))

    def test_json_does_not_accept_non_boolean_metadata(self):
        for change in ({"hostOnly": "false"}, {"session": "true"}):
            self.rejected(json.dumps([cookie(**change)]))

    def test_json_fake_shapes_and_duplicate_object_keys_rejected(self):
        for text in (
            "{'name':'MUSIC_U'}",
            '{"MUSIC_U":"synthetic-session"}',
            '[{"name":"MUSIC_U","name":"other"}]',
            '[{"name":"MUSIC\\_U"}]',
            "[null]",
            "[1]",
            "[[]]",
            "[]",
        ):
            self.rejected(text)

    def test_json_tokens_with_newlines_are_not_repaired(self):
        self.rejected(
            json.dumps([cookie(value="synthetic-session\nX=other")]), "COOKIE_VALUE"
        )

    def test_json_statistics_are_not_authenticated_or_saved(self):
        self.assertEqual(
            self.parsed(
                json.dumps(
                    [
                        cookie(),
                        cookie("_ntes_nnid", "abc,123"),
                        cookie("NMTID", ""),
                        cookie("__csrf", ""),
                    ]
                )
            ),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_netscape_http_only_and_comment_lines(self):
        self.assertEqual(
            self.parsed(
                "# Netscape HTTP Cookie File\n# exporter comment\n#HttpOnly_"
                + netscape()
            ),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_netscape_markdown_escaped_comment_prefix(self):
        self.assertEqual(
            self.parsed("\\#HttpOnly_" + netscape(name=r"MUSIC\_U")),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_netscape_session_zero_and_crlf(self):
        self.assertEqual(
            self.parsed(
                "# Netscape HTTP Cookie File\r\n" + netscape(expiry="0") + "\r\n"
            ),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_netscape_expired_cookie_is_filtered(self):
        self.rejected(netscape(expiry=str(NOW)), "COOKIE_MUSIC_U")

    def test_netscape_empty_optional_last_field_does_not_break_import(self):
        self.assertEqual(
            self.parsed(netscape() + "\n" + netscape("__csrf", "")),
            {"MUSIC_U": "synthetic-session"},
        )

    def test_netscape_single_parent_domain_cookie(self):
        self.assertEqual(
            self.parsed(netscape(domain=".163.com")), {"MUSIC_U": "synthetic-session"}
        )

    def test_netscape_wrong_domain_and_path_rejected(self):
        for changes in (
            {"domain": "evil.example"},
            {"domain": ".163.com", "subdomains": "FALSE"},
            {"path": "/account"},
        ):
            self.rejected(
                "# Netscape HTTP Cookie File\n" + netscape(**changes), "COOKIE_SCOPE"
            )

    def test_netscape_malformed_fields_rejected(self):
        for text in (
            netscape().replace("\t", " "),
            netscape() + "\textra",
            netscape(expiry="NaN"),
            netscape(secure="MAYBE"),
            netscape(subdomains="0"),
        ):
            self.rejected("# Netscape HTTP Cookie File\n" + text)

    def test_netscape_same_priority_conflict_rejected(self):
        self.rejected(
            netscape() + "\n" + netscape(value="different"), "COOKIE_CONFLICT"
        )

    def test_input_size_limit_uses_bytes(self):
        self.rejected("MUSIC_U=synthetic-session;other=" + "x" * 65536, "COOKIE_SIZE")
        self.rejected(
            "MUSIC_U=synthetic-session;other=" + "\u4e2d" * 22000, "COOKIE_SIZE"
        )

    def test_auth_header_size_limit_after_statistics_filtering(self):
        self.assertEqual(
            self.parsed("MUSIC_U=synthetic-session;other=" + "x" * 9000),
            {"MUSIC_U": "synthetic-session"},
        )
        self.rejected("MUSIC_U=" + "x" * 8192, "COOKIE_SIZE")

    def test_export_entry_count_limits(self):
        for text in (
            "MUSIC_U=synthetic-session;" + ";".join("other=x" for _ in range(256)),
            json.dumps([cookie()] * 257),
            "\n".join(netscape() for _ in range(257)),
        ):
            self.rejected(text, "COOKIE_SIZE")

    def test_no_optional_cookie_can_replace_missing_music_u(self):
        for text in (
            "__csrf=csrf;MUSIC_A=guest",
            "MUSIC_U=",
            json.dumps([cookie("__csrf", "csrf")]),
        ):
            self.rejected(text, "COOKIE_MUSIC_U")

    def test_auth_token_punctuation_is_strict_and_not_unquoted(self):
        for value in (
            '"synthetic-session"',
            "synthetic,session",
            "synthetic session",
            "synthetic\\session",
            "synthetic\u4e2dsession",
        ):
            self.rejected(json.dumps([cookie(value=value)]), "COOKIE_VALUE")

    def test_percent_encoded_values_are_preserved_not_decoded(self):
        self.assertEqual(
            self.parsed("MUSIC_U=synthetic%2B%3D%5F"), {"MUSIC_U": "synthetic%2B%3D%5F"}
        )


if __name__ == "__main__":
    unittest.main()
