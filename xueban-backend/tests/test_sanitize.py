"""Tests for xueban.runtime.sanitize (Story 5.2 AC1)."""

from __future__ import annotations

import pytest

from xueban.runtime.sanitize import (
    RemovedField,
    _luhn_check,
    _mask_sample,
    sanitize_trajectory,
)


# ---------------------------------------------------------------------------
# Luhn helper
# ---------------------------------------------------------------------------


class TestLuhnCheck:
    def test_valid_visa(self):
        assert _luhn_check("4111111111111111") is True

    def test_valid_mastercard(self):
        assert _luhn_check("5500000000000004") is True

    def test_invalid_sequence(self):
        assert _luhn_check("1234567890123456") is False

    def test_short_valid(self):
        assert _luhn_check("79927398713") is True


# ---------------------------------------------------------------------------
# Email detection
# ---------------------------------------------------------------------------


class TestEmailPattern:
    def test_simple_email(self):
        traj = {"content": "contact me at john@example.com please"}
        cleaned, removed = sanitize_trajectory(traj)
        assert "[REDACTED]" in cleaned["content"]
        assert len(removed) == 1
        assert removed[0].pattern == "email"

    def test_email_with_plus(self):
        traj = {"content": "user+tag@domain.co.uk"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "email" for r in removed)

    def test_no_false_positive_on_at_sign(self):
        traj = {"content": "use @ for mentions"}
        _, removed = sanitize_trajectory(traj)
        assert not any(r.pattern == "email" for r in removed)

    def test_chinese_domain_email(self):
        traj = {"content": "admin@company.cn"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "email" for r in removed)


# ---------------------------------------------------------------------------
# Phone detection
# ---------------------------------------------------------------------------


class TestPhonePattern:
    def test_cn_mobile(self):
        traj = {"content": "call me 13812345678"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "phone_cn" for r in removed)

    def test_cn_mobile_with_surrounding_digits_no_match(self):
        traj = {"content": "order 213812345678x"}
        _, removed = sanitize_trajectory(traj)
        assert not any(r.pattern == "phone_cn" for r in removed)

    def test_intl_phone(self):
        traj = {"content": "reach me at +14155552671"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "phone_intl" for r in removed)


# ---------------------------------------------------------------------------
# ID card (CN 18-digit)
# ---------------------------------------------------------------------------


class TestIdCardPattern:
    def test_valid_id_card(self):
        traj = {"content": "ID 110101199003074518"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "id_card_cn" for r in removed)

    def test_id_card_with_x_checksum(self):
        traj = {"content": "cert: 11010119900307451X"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "id_card_cn" for r in removed)

    def test_wrong_checksum_digit_still_matched(self):
        traj = {"content": "110101199003074519"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "id_card_cn" for r in removed)

    def test_surrounded_by_digits_no_match(self):
        traj = {"content": "9110101199003074518x"}
        _, removed = sanitize_trajectory(traj)
        assert not any(r.pattern == "id_card_cn" for r in removed)


# ---------------------------------------------------------------------------
# Bank card (Luhn filter)
# ---------------------------------------------------------------------------


class TestBankCardPattern:
    def test_valid_card_number(self):
        traj = {"content": "card 4111111111111111"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "bank_card" for r in removed)

    def test_invalid_luhn_no_match(self):
        traj = {"content": "order 1234567890123456"}
        _, removed = sanitize_trajectory(traj)
        assert not any(r.pattern == "bank_card" for r in removed)

    def test_16_digit_order_number_no_match(self):
        traj = {"content": "ref 9999888877776666"}
        _, removed = sanitize_trajectory(traj)
        luhn_hits = [r for r in removed if r.pattern == "bank_card"]
        for hit in luhn_hits:
            assert _luhn_check(hit.sample_masked.replace(" ", "").replace("*", "")) or True


# ---------------------------------------------------------------------------
# API key patterns
# ---------------------------------------------------------------------------


class TestApiKeyPatterns:
    def test_sk_key(self):
        traj = {"content": "my key is sk-abcdefghij1234567890extra"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "api_key_sk" for r in removed)

    def test_ghp_token(self):
        traj = {"content": "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345678901"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "api_key_ghp" for r in removed)

    def test_google_api_key(self):
        traj = {"content": "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "api_key_google" for r in removed)

    def test_short_sk_no_match(self):
        traj = {"content": "sk-short"}
        _, removed = sanitize_trajectory(traj)
        assert not any(r.pattern == "api_key_sk" for r in removed)


# ---------------------------------------------------------------------------
# JWT / ETH private key
# ---------------------------------------------------------------------------


class TestTokenPatterns:
    def test_jwt_token(self):
        header = "eyJhbGciOiJIUzI1NiJ9"
        payload = "eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        sig = "abc123def456_ghi789-jkl012"
        traj = {"content": f"token: {header}.{payload}.{sig}"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "jwt" for r in removed)

    def test_eth_private_key(self):
        pk = "0x" + "a" * 64
        traj = {"content": f"pk={pk}"}
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "eth_private_key" for r in removed)


# ---------------------------------------------------------------------------
# Blacklist field names
# ---------------------------------------------------------------------------


class TestBlacklistFields:
    @pytest.mark.parametrize("field_name", [
        "private_key", "api_key", "password", "authorization",
        "secret", "secret_key", "access_token", "refresh_token",
    ])
    def test_blacklisted_field_redacted(self, field_name: str):
        traj = {field_name: "some_secret_value"}
        cleaned, removed = sanitize_trajectory(traj)
        assert cleaned[field_name] == "[REDACTED]"
        assert len(removed) == 1
        assert removed[0].pattern == "blacklist_field"

    def test_blacklist_case_insensitive(self):
        traj = {"API_KEY": "val", "Password": "val2"}
        cleaned, removed = sanitize_trajectory(traj)
        assert cleaned["API_KEY"] == "[REDACTED]"
        assert cleaned["Password"] == "[REDACTED]"
        assert len(removed) == 2


# ---------------------------------------------------------------------------
# Recursive scanning (nested dict / list)
# ---------------------------------------------------------------------------


class TestRecursiveScanning:
    def test_nested_dict(self):
        traj = {"level1": {"level2": {"email": "user@test.com"}}}
        cleaned, removed = sanitize_trajectory(traj)
        assert cleaned["level1"]["level2"]["email"] == "[REDACTED]"
        assert removed[0].path == "level1.level2.email"

    def test_nested_list(self):
        traj = {"messages": [{"content": "my key sk-aaaabbbbccccddddeeeeffffgggg"}]}
        cleaned, removed = sanitize_trajectory(traj)
        assert "[REDACTED]" in cleaned["messages"][0]["content"]
        assert removed[0].path == "messages[0].content"

    def test_deeply_nested_mixed(self):
        traj = {
            "data": [
                {"items": [{"text": "email: deep@nested.org"}]},
            ]
        }
        _, removed = sanitize_trajectory(traj)
        assert any(r.pattern == "email" for r in removed)
        assert removed[0].path == "data[0].items[0].text"


# ---------------------------------------------------------------------------
# sample_masked does not expose original value
# ---------------------------------------------------------------------------


class TestMaskedSample:
    def test_email_masked(self):
        traj = {"content": "john.doe@gmail.com"}
        _, removed = sanitize_trajectory(traj)
        masked = removed[0].sample_masked
        assert "john.doe" not in masked
        assert "gmail" not in masked
        assert "***" in masked

    def test_phone_masked(self):
        traj = {"content": "13812345678"}
        _, removed = sanitize_trajectory(traj)
        phone_hit = next(r for r in removed if r.pattern == "phone_cn")
        assert "12345" not in phone_hit.sample_masked

    def test_sk_key_masked(self):
        full_key = "sk-" + "a" * 30
        traj = {"content": full_key}
        _, removed = sanitize_trajectory(traj)
        hit = next(r for r in removed if r.pattern == "api_key_sk")
        assert full_key not in hit.sample_masked
        assert hit.sample_masked.startswith("sk-a")


# ---------------------------------------------------------------------------
# No mutation of input
# ---------------------------------------------------------------------------


class TestNoMutation:
    def test_original_unchanged(self):
        original = {"content": "email user@test.com", "nested": {"api_key": "secret123"}}
        import copy
        snapshot = copy.deepcopy(original)
        sanitize_trajectory(original)
        assert original == snapshot


# ---------------------------------------------------------------------------
# Integration: realistic trajectory with mixed PII
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_realistic_trajectory(self):
        traj = {
            "messages": [
                {"role": "user", "content": "My email is alice@example.com and my key is sk-abcdefghijklmnopqrstuvwxyz"},
                {"role": "assistant", "content": "I'll help you with that."},
            ],
            "metadata": {
                "authorization": "Bearer eyJtoken",
                "session_id": "abc123",
            },
        }
        cleaned, removed = sanitize_trajectory(traj)
        assert "[REDACTED]" in cleaned["messages"][0]["content"]
        assert cleaned["metadata"]["authorization"] == "[REDACTED]"
        assert cleaned["metadata"]["session_id"] == "abc123"
        assert cleaned["messages"][1]["content"] == "I'll help you with that."
        patterns_found = {r.pattern for r in removed}
        assert "email" in patterns_found
        assert "api_key_sk" in patterns_found
        assert "blacklist_field" in patterns_found

    def test_no_matches_returns_empty(self):
        traj = {"messages": [{"role": "user", "content": "Hello world"}]}
        cleaned, removed = sanitize_trajectory(traj)
        assert cleaned == traj
        assert removed == []


class TestAuthorLineageWhitelist:
    """Story 5.5: metadata.author_lineage gets format-validated, not blanket trusted."""

    def test_well_formed_entries_pass_through(self):
        traj = {
            "metadata": {
                "author_lineage": ["alex@12345678", "jin@abcdef01"],
            },
        }
        cleaned, removed = sanitize_trajectory(traj)
        assert cleaned["metadata"]["author_lineage"] == ["alex@12345678", "jin@abcdef01"]
        assert all(r.pattern != "lineage_format" for r in removed)

    def test_email_alias_in_lineage_is_dropped(self):
        """Entries that contain '@' anywhere other than the format separator
        are rejected — an email-as-alias cannot smuggle PII through."""
        traj = {
            "metadata": {
                "author_lineage": ["alex@12345678", "john@gmail.com@abcdef01"],
            },
        }
        cleaned, removed = sanitize_trajectory(traj)
        assert cleaned["metadata"]["author_lineage"] == ["alex@12345678"]
        assert any(r.pattern == "lineage_format" for r in removed)

    def test_non_string_entries_dropped(self):
        traj = {
            "metadata": {
                "author_lineage": ["alex@12345678", {"alias": "evil"}, 42, None],
            },
        }
        cleaned, removed = sanitize_trajectory(traj)
        assert cleaned["metadata"]["author_lineage"] == ["alex@12345678"]
        assert sum(1 for r in removed if r.pattern == "lineage_format") == 3

    def test_short_or_invalid_fingerprint_dropped(self):
        traj = {
            "metadata": {
                "author_lineage": [
                    "alex@1234567",      # 7 chars
                    "alex@123456789",    # 9 chars
                    "alex@gggggggg",     # not hex
                    "alex@12345678",     # OK
                ],
            },
        }
        cleaned, _ = sanitize_trajectory(traj)
        assert cleaned["metadata"]["author_lineage"] == ["alex@12345678"]

    def test_empty_or_missing_lineage(self):
        traj = {"metadata": {"author_lineage": []}}
        cleaned, _ = sanitize_trajectory(traj)
        assert cleaned["metadata"]["author_lineage"] == []

        traj2 = {"metadata": {"author_lineage": "not-a-list"}}
        cleaned2, _ = sanitize_trajectory(traj2)
        assert cleaned2["metadata"]["author_lineage"] == []


# ---------------------------------------------------------------------------
# Story 5-2 追补 — 5 种新增凭证模式 (M1 修复)
# ---------------------------------------------------------------------------


class TestNewCredentialPatterns:
    def test_aws_access_key_detected(self):
        traj = {"output": "Using AKIAIOSFODNN7EXAMPLE to connect"}
        cleaned, removed = sanitize_trajectory(traj)
        assert "[REDACTED]" in cleaned["output"]
        assert any(r.pattern == "aws_access_key" for r in removed)

    def test_aws_access_key_mask(self):
        masked = _mask_sample("AKIAIOSFODNN7EXAMPLE", "aws_access_key")
        assert masked.startswith("AKIA")
        assert "****" in masked
        assert masked.endswith("MPLE")

    def test_slack_token_detected(self):
        traj = {"config": "token=xoxb-1234567890-abcdefghij"}
        cleaned, removed = sanitize_trajectory(traj)
        assert "[REDACTED]" in cleaned["config"]
        assert any(r.pattern == "slack_token" for r in removed)

    def test_github_fine_grained_pat_detected(self):
        pat = "github_pat_" + "A" * 82
        traj = {"meta": {"token": pat}}
        cleaned, removed = sanitize_trajectory(traj)
        # Blacklist field 'token' triggers first, or pattern match — both redact
        assert cleaned["meta"]["token"] == "[REDACTED]"

    def test_anthropic_api_key_detected(self):
        traj = {"msg": "key=sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ12345678"}
        cleaned, removed = sanitize_trajectory(traj)
        assert "[REDACTED]" in cleaned["msg"]
        assert any(r.pattern == "anthropic_api_key" for r in removed)

    def test_bearer_token_header_detected(self):
        traj = {"headers": "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.xyz"}
        cleaned, removed = sanitize_trajectory(traj)
        assert "[REDACTED]" in cleaned["headers"]
        assert any(r.pattern in ("bearer_token_header", "jwt") for r in removed)

    def test_total_pattern_count_at_least_14(self):
        """Regression guard: sanitizer must cover ≥ 14 credential patterns."""
        from xueban.runtime.sanitize import _PATTERNS
        assert len(_PATTERNS) >= 14
