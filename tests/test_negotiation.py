"""Unit tests for Accept-Language parsing and negotiation core."""

import pytest

from app.negotiation import (
    AcceptLanguageError,
    LocaleFallbackError,
    ParticipantHeaderError,
    SampleHeaderError,
    analyze_q_stability,
    negotiate,
    negotiate_group,
    normalize_fallbacks,
    parse_accept_language,
    plan_coverage,
    truncation_chain,
    validate_fallback_chains,
)


class TestParse:
    def test_none_and_empty(self):
        assert parse_accept_language(None) == []
        assert parse_accept_language("   ") == []

    def test_basic_q_weights_and_order(self):
        ranges = parse_accept_language("zh-CN,zh;q=0.9,en;q=0.8")
        assert [(r.range, r.q, r.position) for r in ranges] == [
            ("zh-cn", 1.0, 1),
            ("zh", 0.9, 2),
            ("en", 0.8, 3),
        ]

    def test_wildcard(self):
        ranges = parse_accept_language("*;q=0.5")
        assert ranges[0].range == "*"
        assert ranges[0].q == 0.5

    def test_duplicate_ranges_keep_first(self):
        ranges = parse_accept_language("en;q=0.8,EN;q=0.2, fr")
        assert [(r.range, r.q) for r in ranges] == [("en", 0.8), ("fr", 1.0)]

    def test_q_zero_preserved(self):
        ranges = parse_accept_language("en;q=0")
        assert ranges[0].q == 0.0

    @pytest.mark.parametrize(
        "header",
        [
            "en;q=",  # missing q value
            "en;q=2",  # out of range
            "en;q=1.0001",  # too many decimals
            "en;q=.5",  # leading dot forbidden
            "en;r=0.5",  # unknown parameter
            "en;q;q",  # q without value
            "not a tag",  # whitespace in tag
            "12345",  # not BCP 47
            "en,,fr",  # empty element
            "en-",  # dangling separator
            "en;;fr",  # empty parameter
        ],
    )
    def test_invalid_syntax(self, header):
        with pytest.raises(AcceptLanguageError):
            parse_accept_language(header)

    def test_ows_around_elements_and_params_allowed(self):
        # RFC 9110 allows OWS around elements, ";" and "=".
        ranges = parse_accept_language(" en , fr ; q = 0.5 ")
        assert [(r.range, r.q) for r in ranges] == [("en", 1.0), ("fr", 0.5)]


class TestTruncationChain:
    def test_basic(self):
        assert truncation_chain("zh-Hant-TW") == [
            "zh-hant-tw",
            "zh-hant",
            "zh",
        ]

    def test_singleton_prefixes_skipped_but_intermediate_kept(self):
        # RFC 4647 truncates one subtag at a time and only skips prefixes
        # whose trailing subtag is a singleton.
        assert truncation_chain("zh-CN-u-ca-chinese") == [
            "zh-cn-u-ca-chinese",
            "zh-cn-u-ca",
            "zh-cn",
            "zh",
        ]

    def test_privateuse_singleton(self):
        assert truncation_chain("en-US-x-foo") == [
            "en-us-x-foo",
            "en-us",
            "en",
        ]

    def test_pure_privateuse_tag(self):
        assert truncation_chain("x-foo-bar") == ["x-foo-bar", "x-foo"]


class TestNegotiate:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    def test_weights_and_truncation(self):
        result = negotiate(
            "zh-TW;q=0.95,zh;q=0.9,en;q=0.8,*;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "zh-TW"
        assert result["fallback"] is False
        # zh-CN truncates to zh -> matched by 'zh' range at q 0.9.
        zh_cn = next(c for c in result["candidates"] if c["tag"] == "zh-CN")
        assert zh_cn["matched_range"] == "zh"
        assert zh_cn["q"] == 0.9
        # ja-JP only matches wildcard.
        ja = next(c for c in result["candidates"] if c["tag"] == "ja-JP")
        assert ja["matched_range"] == "*"
        assert ja["q"] == 0.5
        match_events = [e for e in ja["events"] if e["outcome"] == "match"]
        assert match_events[-1]["truncated_to"] == "*"

    def test_q_zero_excludes_tag(self):
        result = negotiate(
            "en;q=0,zh-CN;q=0.9", self.SUPPORTED, self.DEFAULT
        )
        en = result["candidates"][0]
        assert en["status"] == "excluded"
        assert en["q"] == 0
        assert result["selected_language"] == "zh-CN"

    def test_q_zero_blocks_wildcard_excluded_candidates(self):
        # All explicit ranges q=0 -> default fallback even with wildcard q=1
        # concrete en q=0 excludes en; ja has wildcard q=1 though...
        result = negotiate(
            "en;q=0,ja;q=0,*;q=0.1", self.SUPPORTED, self.DEFAULT
        )
        assert result["selected_language"] == "zh-CN"
        assert result["fallback"] is False

    def test_all_excluded_falls_back_to_default(self):
        result = negotiate("*;q=0", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-acceptable-candidate"
        assert "q=0" in result["message"]

    def test_missing_header_falls_back(self):
        for header in (None, "", "   "):
            result = negotiate(header, self.SUPPORTED, self.DEFAULT)
            assert result["selected_language"] == "en-US"
            assert result["fallback_reason"] == "no-valid-range"
            assert all(c["status"] == "no-range" for c in result["candidates"])

    def test_tie_breaks_by_server_order(self):
        # en and zh both match * at q=0.1 -> earliest in supported list wins.
        result = negotiate("de;q=0.9,*;q=0.1", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"

    def test_exact_match_beats_prefix_and_wildcard(self):
        supported = ["en", "en-US"]
        result = negotiate("en-US;q=0.1,en;q=0.9,*;q=0.99", supported, "en")
        # en-US exact q=0.1 vs en exact q=0.9: highest q wins -> en.
        assert result["selected_language"] == "en"
        en_us = result["candidates"][1]
        assert en_us["matched_range"] == "en-us"
        # wildcard event skipped because concrete match exists
        assert all(
            e["outcome"] != "match" or e["truncated_to"] != "*"
            for e in en_us["events"]
        )

    def test_duplicate_header_ranges_reported(self):
        result = negotiate(
            "en;q=0.8,en;q=0.2", self.SUPPORTED, self.DEFAULT
        )
        assert len(result["parsed_ranges"]) == 1
        assert result["ignored_duplicates"] == [
            {"range": "en", "position": 2}
        ]
        assert result["candidates"][0]["q"] == 0.8

    def test_wildcard_only_selects_first_supported(self):
        result = negotiate("*", self.SUPPORTED, self.DEFAULT)
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is False

    def test_default_language_case_insensitive_lookup(self):
        # The schema accepts a default that differs in case from the
        # supported list entry; the fallback lookup must not crash on it.
        result = negotiate("fr", ["en-US"], "EN-us")
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True


class TestFallbackValidation:
    def test_normalize_lowercases_and_accepts_valid(self):
        assert normalize_fallbacks({"DE": "EN-gb", "en-gb": "en"}) == {
            "de": "en-gb",
            "en-gb": "en",
        }

    @pytest.mark.parametrize(
        "raw",
        [
            [("de", "en")],  # not a mapping
            {"de": "en", "DE": "fr"},  # duplicate source case-insensitive
            {"12345": "en"},  # bad source tag
            {"de": "not a tag"},  # bad target tag
        ],
    )
    def test_normalize_rejects_bad_shape(self, raw):
        with pytest.raises(LocaleFallbackError):
            normalize_fallbacks(raw)

    def test_chain_reaching_supported_is_valid(self):
        validate_fallback_chains(
            {"de": "gsw", "gsw": "zh-cn"}, ["zh-CN", "en-US"]
        )

    def test_dangling_chain_rejected(self):
        with pytest.raises(LocaleFallbackError, match="悬空"):
            validate_fallback_chains({"de": "gsw"}, ["zh-CN"])

    def test_cycle_rejected(self):
        with pytest.raises(LocaleFallbackError, match="环路"):
            validate_fallback_chains(
                {"de": "fr", "fr": "it", "it": "fr"}, ["zh-CN"]
            )

    def test_self_loop_before_supported_rejected(self):
        with pytest.raises(LocaleFallbackError, match="环路"):
            validate_fallback_chains({"de": "de"}, ["zh-CN"])

    def test_self_loop_on_supported_tag_ok(self):
        validate_fallback_chains({"en": "en", "de": "en"}, ["en"])


class TestLocaleChainFallback:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    def test_chain_walked_from_positive_q_range(self):
        result = negotiate(
            "gsw;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            {"gsw": "de", "de": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["fallback"] is False
        report = result["locale_fallback"]
        assert report["triggered"] is True
        assert report["source_range"] == "gsw"
        assert report["source_q"] == 0.9
        assert report["source_position"] == 1
        assert report["path"] == ["gsw", "de", "zh-cn"]
        assert report["selected_language"] == "zh-CN"
        winner = next(
            c for c in result["candidates"] if c["tag"] == "zh-CN"
        )
        assert winner["status"] == "selected"

    def test_sources_ordered_by_descending_q(self):
        # Both ranges miss; de (higher q) is tried first even though fr
        # appears earlier in the header.
        result = negotiate(
            "fr;q=0.3,de;q=0.9",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-TW", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "zh-TW"
        assert result["locale_fallback"]["source_range"] == "de"

    def test_equal_q_breaks_by_header_order(self):
        result = negotiate(
            "fr;q=0.5,de;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-TW", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "ja-JP"
        assert result["locale_fallback"]["source_range"] == "fr"

    def test_q_zero_supported_node_skipped_walk_continues(self):
        result = negotiate(
            "de,zh-tw;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-TW", "zh-tw": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["path"] == [
            "de",
            "zh-tw",
            "zh-cn",
        ]

    def test_q_zero_prefix_excludes_supported_node(self):
        # zh;q=0 excludes zh-CN via lookup prefix as well.
        result = negotiate(
            "de,zh;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN", "zh-cn": "ja-JP"},
        )
        assert result["selected_language"] == "ja-JP"
        assert result["locale_fallback"]["path"] == [
            "de",
            "zh-cn",
            "ja-jp",
        ]

    def test_wildcard_q_zero_does_not_block_chain(self):
        # Only concrete q=0 ranges disqualify chain candidates.
        result = negotiate(
            "de,*;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["path"] == ["de", "zh-cn"]

    def test_q_zero_range_never_starts_a_chain(self):
        result = negotiate(
            "de;q=0,fr;q=0.5",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN", "fr": "ja-JP"},
        )
        assert result["selected_language"] == "ja-JP"
        assert result["locale_fallback"]["source_range"] == "fr"

    def test_dead_end_source_then_next_source_tried(self):
        result = negotiate(
            "de,ja-jp;q=0,fr",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "ja-JP", "ja-jp": "ko", "fr": "zh-CN"},
        )
        assert result["selected_language"] == "zh-CN"
        assert result["locale_fallback"]["source_range"] == "fr"

    def test_chains_not_consulted_when_standard_lookup_eligible(self):
        result = negotiate(
            "en;q=0.1,*;q=0.05",
            self.SUPPORTED,
            self.DEFAULT,
            {"zz": "zh-TW"},
        )
        assert result["selected_language"] == "en-US"
        assert result["locale_fallback"] is None

    def test_no_reachable_chain_falls_back_to_default(self):
        result = negotiate(
            "de;q=0",
            self.SUPPORTED,
            self.DEFAULT,
            {"de": "zh-CN"},
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-acceptable-candidate"
        assert result["locale_fallback"] is None
        assert "locale_fallbacks" in result["message"]

    def test_without_fallbacks_key_behavior_unchanged(self):
        result = negotiate("de", self.SUPPORTED, self.DEFAULT)
        assert "locale_fallback" in result
        assert result["locale_fallback"] is None


class TestGroupNegotiate:
    SUPPORTED = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
    DEFAULT = "en-US"

    @staticmethod
    def _candidate(result, tag):
        return next(c for c in result["candidates"] if c["tag"] == tag)

    def test_min_weight_wins_over_total(self):
        # en-US has the higher total but the lower minimum -> zh-CN wins.
        result = negotiate_group(
            [("alice", "zh;q=0.6,en;q=0.5"), ("bob", "zh;q=0.7,en;q=1.0")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "zh-CN"
        assert result["fallback"] is False
        zh = self._candidate(result, "zh-CN")
        en = self._candidate(result, "en-US")
        assert zh["min_q"] == 0.6 and zh["total_q"] == 1.3
        assert en["min_q"] == 0.5 and en["total_q"] == 1.5
        assert zh["status"] == "selected"
        assert en["status"] == "eligible"

    def test_total_weight_breaks_min_tie(self):
        # Equal minima: zh-TW wins on total despite its later server position.
        result = negotiate_group(
            [
                ("alice", "zh-tw;q=0.9,zh-cn;q=0.8"),
                ("bob", "zh-tw;q=0.8,zh-cn;q=0.8"),
            ],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "zh-TW"
        zh_tw = self._candidate(result, "zh-TW")
        zh_cn = self._candidate(result, "zh-CN")
        assert zh_tw["min_q"] == zh_cn["min_q"] == 0.8
        assert zh_tw["total_q"] == 1.7
        assert zh_cn["total_q"] == 1.6

    def test_supported_order_breaks_full_tie(self):
        result = negotiate_group(
            [("alice", "zh;q=0.8"), ("bob", "zh;q=0.8")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        # zh-CN and zh-TW have identical weights -> earlier supported tag.
        assert result["selected_language"] == "zh-CN"

    def test_concrete_q_zero_vetoes_globally(self):
        result = negotiate_group(
            [("alice", "zh;q=0,*;q=0.6"), ("bob", "zh;q=0.9,*;q=0.7")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "en-US"
        zh = self._candidate(result, "zh-CN")
        assert zh["vetoed"] is True
        assert zh["vetoed_by"] == ["alice"]
        assert zh["status"] == "vetoed"
        # Alice's concrete q=0 hit is recorded as her matched range...
        assert zh["weights"][0] == {
            "participant_id": "alice",
            "q": 0.0,
            "matched_range": "zh",
            "veto": True,
        }
        # ...while bob still likes zh-CN at 0.9; the veto overrides him.
        assert zh["weights"][1]["q"] == 0.9
        assert zh["weights"][1]["veto"] is False

    def test_wildcard_q_zero_is_not_a_veto(self):
        result = negotiate_group(
            [("alice", "*;q=0"), ("bob", "en;q=0.9,zh;q=0.8")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        # Alice zeroes every weight via the wildcard but vetoes nothing.
        assert all(c["vetoed"] is False for c in result["candidates"])
        assert all(c["vetoed_by"] == [] for c in result["candidates"])
        # Every candidate still has a zero minimum -> default fallback.
        assert all(c["status"] == "unacceptable" for c in result["candidates"])
        assert result["selected_language"] == self.DEFAULT
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-common-acceptable-candidate"

    def test_unmatched_candidate_counts_zero(self):
        result = negotiate_group(
            [("alice", "en"), ("bob", "en;q=0.5")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "en-US"
        zh = self._candidate(result, "zh-CN")
        assert zh["min_q"] == 0.0
        assert zh["total_q"] == 0.0
        assert zh["status"] == "unacceptable"
        assert all(w["matched_range"] is None for w in zh["weights"])

    def test_null_header_participant_abstains(self):
        result = negotiate_group(
            [("alice", None), ("bob", "zh;q=0.8")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "zh-CN"
        alice = result["participants"][0]
        assert alice["abstained"] is True
        assert alice["parsed_ranges"] == []
        zh = self._candidate(result, "zh-CN")
        assert zh["weights"][0] == {
            "participant_id": "alice",
            "q": 1.0,
            "matched_range": None,
            "veto": False,
        }
        assert zh["min_q"] == 0.8

    def test_all_abstain_picks_first_supported(self):
        result = negotiate_group(
            [("alice", None), ("bob", "   ")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is False
        assert all(p["abstained"] for p in result["participants"])
        en = result["candidates"][0]
        assert en["min_q"] == 1.0 and en["total_q"] == 2.0

    def test_no_common_candidate_falls_back_to_default(self):
        result = negotiate_group(
            [("alice", "en;q=0"), ("bob", "zh;q=0,ja;q=0")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True
        assert result["fallback_reason"] == "no-common-acceptable-candidate"
        # The default itself is vetoed by alice: the message says so.
        assert "默认语言本身" in result["message"]

    def test_group_default_case_insensitive_lookup(self):
        result = negotiate_group([("alice", "en;q=0")], ["en-US"], "EN-us")
        assert result["selected_language"] == "en-US"
        assert result["fallback"] is True

    def test_per_participant_matched_ranges(self):
        result = negotiate_group(
            [("alice", "zh-CN;q=0.9"), ("bob", "zh;q=0.7")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        zh = self._candidate(result, "zh-CN")
        assert [w["matched_range"] for w in zh["weights"]] == ["zh-cn", "zh"]
        en = self._candidate(result, "en-US")
        assert [w["matched_range"] for w in en["weights"]] == [None, None]

    def test_duplicate_ranges_reported_per_participant(self):
        result = negotiate_group(
            [("alice", "en;q=0.8,EN;q=0.2"), ("bob", "en")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        alice = result["participants"][0]
        assert alice["ignored_duplicates"] == [{"range": "en", "position": 2}]
        assert result["candidates"][0]["weights"][0]["q"] == 0.8

    def test_invalid_participant_header_raises(self):
        with pytest.raises(ParticipantHeaderError) as excinfo:
            negotiate_group(
                [("alice", "en"), ("bob", "fr;q=2")],
                self.SUPPORTED,
                self.DEFAULT,
            )
        exc = excinfo.value
        assert isinstance(exc, AcceptLanguageError)
        assert exc.participant_id == "bob"
        assert exc.position == 2
        assert "bob" in str(exc)

    def test_total_q_has_no_float_noise(self):
        result = negotiate_group(
            [("alice", "en;q=0.1"), ("bob", "en;q=0.2")],
            self.SUPPORTED,
            self.DEFAULT,
        )
        assert result["candidates"][0]["total_q"] == 0.3


class TestCoveragePlan:
    def test_basic_improvement(self):
        result = plan_coverage(
            samples=[("fr;q=0.9", 100.0), ("de", 50.0), ("en", 200.0)],
            supported=["en-US"],
            default="en-US",
            candidates=["fr-FR", "de-DE"],
            max_additions=2,
        )
        assert result["selected_additions"] == ["fr-FR", "de-DE"]
        assert result["baseline_fallback_traffic"] == 150.0
        assert result["improved_traffic"] == 150.0
        reports = result["samples"]
        assert [r["position"] for r in reports] == [1, 2, 3]
        assert [r["improved"] for r in reports] == [True, True, False]
        assert reports[0]["baseline_selected_language"] == "en-US"
        assert reports[0]["baseline_fallback"] is True
        assert reports[0]["planned_selected_language"] == "fr-FR"
        assert reports[0]["planned_fallback"] is False
        # Already a normal hit in the baseline: unchanged, not improved.
        assert reports[2]["baseline_fallback"] is False
        assert reports[2]["planned_selected_language"] == "en-US"

    def test_tie_prefers_fewer_additions(self):
        result = plan_coverage(
            samples=[("fr", 100.0)],
            supported=["en-US"],
            default="en-US",
            candidates=["fr-FR", "es-ES"],
            max_additions=2,
        )
        assert result["selected_additions"] == ["fr-FR"]
        assert result["improved_traffic"] == 100.0

    def test_tie_prefers_earlier_candidate_input_order(self):
        result = plan_coverage(
            samples=[("de,fr", 80.0)],
            supported=["en-US"],
            default="en-US",
            candidates=["de-DE", "fr-FR"],
            max_additions=1,
        )
        assert result["selected_additions"] == ["de-DE"]

    def test_no_improvement_recommends_nothing(self):
        # fr is q=0-excluded even once added; a headerless sample always
        # falls back to the default regardless of the supported list.
        result = plan_coverage(
            samples=[("fr;q=0", 60.0), (None, 30.0)],
            supported=["en-US"],
            default="en-US",
            candidates=["fr-FR", "de-DE"],
            max_additions=2,
        )
        assert result["selected_additions"] == []
        assert result["improved_traffic"] == 0.0
        assert result["baseline_fallback_traffic"] == 90.0
        assert [r["improved"] for r in result["samples"]] == [False, False]
        assert all(r["planned_fallback"] for r in result["samples"])

    def test_traffic_sums_have_no_float_noise(self):
        result = plan_coverage(
            samples=[("fr", 0.1), ("de", 0.2)],
            supported=["en-US"],
            default="en-US",
            candidates=["fr-FR", "de-DE"],
            max_additions=2,
        )
        assert result["baseline_fallback_traffic"] == 0.3
        assert result["improved_traffic"] == 0.3

    def test_invalid_sample_header_raises_with_position(self):
        with pytest.raises(SampleHeaderError) as excinfo:
            plan_coverage(
                samples=[("en", 1.0), ("fr;q=2", 1.0)],
                supported=["en-US"],
                default="en-US",
                candidates=["fr-FR"],
                max_additions=1,
            )
        exc = excinfo.value
        assert isinstance(exc, AcceptLanguageError)
        assert exc.position == 2


class TestQStability:
    def test_baseline_matches_negotiate(self):
        result = analyze_q_stability(
            header="zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5",
            supported=["en-US", "zh-CN", "zh-TW", "ja-JP"],
            default="en-US",
        )
        expected = negotiate(
            header="zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5",
            supported=["en-US", "zh-CN", "zh-TW", "ja-JP"],
            default="en-US",
        )
        assert result["baseline"] == expected
        assert result["baseline"]["selected_language"] == "zh-CN"

    def test_reports_one_entry_per_deduplicated_range_in_header_order(self):
        result = analyze_q_stability(
            header="en;q=0.5,EN;q=0.2, zh;q=0.8,*;q=0.1",
            supported=["en-US", "zh-CN"],
            default="en-US",
        )
        assert [
            (r["range"], r["position"], r["original_q"])
            for r in result["ranges"]
        ] == [
            ("en", 1, 0.5),
            ("zh", 3, 0.8),
            ("*", 4, 0.1),
        ]

    def test_winner_range_interval_breaks_at_tie_point(self):
        # zh-CN (q=1) beats zh-TW (matched via zh;q=0.9) while strictly
        # greater; at 0.9 the server-order tiebreak flips to zh-TW.
        result = analyze_q_stability(
            header="zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5",
            supported=["en-US", "zh-CN", "zh-TW", "ja-JP"],
            default="en-US",
        )
        report = result["ranges"][0]
        assert report["range"] == "zh-cn"
        assert report["original_q"] == 1.0
        assert report["lower_bound"] == 0.9
        assert report["upper_bound"] == 1.0
        assert report["upper_outside"] is None
        assert report["lower_outside"] == {
            "q": 0.899,
            "selected_language": "zh-TW",
            "fallback": False,
        }

    def test_competitor_range_interval_breaks_when_it_takes_over(self):
        # en-US (en;q=0.8) loses to zh-CN (zh;q=0.9); since en-US is the
        # earliest supported tag it wins the tie once en reaches q=0.9.
        result = analyze_q_stability(
            header="zh;q=0.9,en;q=0.8,*;q=0.5",
            supported=["en-US", "zh-CN", "zh-TW"],
            default="en-US",
        )
        report = next(r for r in result["ranges"] if r["range"] == "en")
        assert report["lower_bound"] == 0.0
        assert report["upper_bound"] == 0.899
        assert report["lower_outside"] is None
        assert report["upper_outside"] == {
            "q": 0.9,
            "selected_language": "en-US",
            "fallback": False,
        }

    def test_wildcard_q_zero_exclusion_boundary(self):
        # With only "*;q=0.5" every candidate is wildcard-matched and
        # en-US wins normally; at q=0 all are excluded -> default fallback.
        result = analyze_q_stability(
            header="*;q=0.5",
            supported=["en-US", "zh-CN"],
            default="en-US",
        )
        report = result["ranges"][0]
        assert report["range"] == "*"
        assert report["lower_bound"] == 0.001
        assert report["upper_bound"] == 1.0
        assert report["upper_outside"] is None
        assert report["lower_outside"] == {
            "q": 0.0,
            "selected_language": "en-US",
            "fallback": True,
        }

    def test_concrete_q_zero_range_original_point_is_at_zero(self):
        # Only the default tag en-US matches, and it is q=0-excluded, so
        # the baseline falls back; the moment en gets any positive weight
        # it wins normally (fallback flips), making 0.000 a stable point.
        result = analyze_q_stability(
            header="en;q=0",
            supported=["en-US", "fr-FR"],
            default="en-US",
        )
        report = next(r for r in result["ranges"] if r["range"] == "en")
        assert result["baseline"]["selected_language"] == "en-US"
        assert result["baseline"]["fallback"] is True
        assert report["original_q"] == 0.0
        assert report["lower_bound"] == 0.0
        assert report["upper_bound"] == 0.0
        assert report["lower_outside"] is None
        assert report["upper_outside"] == {
            "q": 0.001,
            "selected_language": "en-US",
            "fallback": False,
        }

    def test_truncation_matching_via_negotiate(self):
        # Raising zh-Hant's q above the zh-CN direct q moves the winner;
        # zh-Hant matches zh-Hant-TW via truncation, just like negotiate().
        result = analyze_q_stability(
            header="zh-CN;q=0.8,zh-Hant;q=0.7",
            supported=["en-US", "zh-CN", "zh-Hant-TW"],
            default="en-US",
        )
        report = next(r for r in result["ranges"] if r["range"] == "zh-hant")
        assert result["baseline"]["selected_language"] == "zh-CN"
        assert report["lower_bound"] == 0.0
        assert report["upper_bound"] == 0.8
        assert report["upper_outside"] == {
            "q": 0.801,
            "selected_language": "zh-Hant-TW",
            "fallback": False,
        }

    def test_locale_fallback_state_changes_at_q_zero(self):
        # The gsw chain selects zh-TW for every strictly positive q;
        # q=0 removes the positive source range -> default fallback.
        result = analyze_q_stability(
            header="gsw;q=0.9",
            supported=["en-US", "zh-TW"],
            default="en-US",
            locale_fallbacks={"gsw": "zh-TW"},
        )
        report = result["ranges"][0]
        assert result["baseline"]["selected_language"] == "zh-TW"
        assert result["baseline"]["fallback"] is False
        assert report["lower_bound"] == 0.001
        assert report["upper_bound"] == 1.0
        assert report["lower_outside"] == {
            "q": 0.0,
            "selected_language": "en-US",
            "fallback": True,
        }

    def test_blank_header_has_no_range_reports(self):
        result = analyze_q_stability(
            header=None,
            supported=["en-US", "zh-CN"],
            default="en-US",
        )
        assert result["ranges"] == []
        assert result["baseline"]["fallback"] is True
        assert result["baseline"]["fallback_reason"] == "no-valid-range"

    def test_invalid_header_raises(self):
        with pytest.raises(AcceptLanguageError):
            analyze_q_stability(
                header="en;q=2",
                supported=["en-US"],
                default="en-US",
            )

    def test_other_ranges_keep_original_tokens_during_scan(self):
        # The scanned element keeps its raw spelling; a duplicate later
        # occurrence remains ignored (negotiate dedupes) and a wildcard
        # keeps its own q throughout the scan.
        result = analyze_q_stability(
            header="en;q=0.5,en;q=0.2,*;q=0.5",
            supported=["en-US", "zh-CN"],
            default="en-US",
        )
        report = next(r for r in result["ranges"] if r["range"] == "en")
        # en-US baseline-wins over the wildcard tie only by concreteness;
        # lowering en below 0.5 lets the wildcard candidate zh-CN win.
        assert report["lower_bound"] == 0.5
        assert report["lower_outside"] == {
            "q": 0.499,
            "selected_language": "zh-CN",
            "fallback": False,
        }
        assert [r["range"] for r in result["ranges"]] == ["en", "*"]

    def test_intervals_match_brute_force_with_locale_chains(self):
        header = "gsw;q=0.9,fr;q=0.5"
        supported = ["en-US", "zh-TW", "ja-JP"]
        default = "en-US"
        chains = {"gsw": "de", "de": "zh-TW", "fr": "ja-JP"}
        result = analyze_q_stability(
            header=header,
            supported=supported,
            default=default,
            locale_fallbacks=chains,
        )
        parsed = parse_accept_language(header)
        elements = header.split(",")
        base_key = (
            result["baseline"]["selected_language"],
            result["baseline"]["fallback"],
        )

        for entry, report in zip(parsed, result["ranges"]):
            def brute(step: int) -> tuple[str, bool]:
                token = f"{step // 1000}.{step % 1000:03d}"
                parts = elements[entry.position - 1].split(";")
                head, _sep, _value = parts[1].partition("=")
                parts[1] = head + "=" + token
                rewritten = ",".join(
                    elements[: entry.position - 1]
                    + [";".join(parts)]
                    + elements[entry.position :]
                )
                outcome = negotiate(
                    rewritten, supported, default, locale_fallbacks=chains
                )
                return outcome["selected_language"], outcome["fallback"]

            original_step = round(entry.q * 1000)
            lower = original_step
            while lower - 1 >= 0 and brute(lower - 1) == base_key:
                lower -= 1
            upper = original_step
            while upper + 1 <= 1000 and brute(upper + 1) == base_key:
                upper += 1
            assert report["lower_bound"] == lower / 1000
            assert report["upper_bound"] == upper / 1000

    @pytest.mark.parametrize(
        "header",
        [
            "zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5",
            "en;q=0,*;q=0.6",
            "*;q=0.5",
            "en;q=0",
            "de;q=0.9,en;q=0.1,*;q=0.05",
            "zh-CN;q=0.8,zh-TW;q=0.799",
            "fr;q=0.333,de;q=0.334,en;q=0.333",
        ],
    )
    def test_intervals_match_brute_force_grid_walk(self, header):
        # Property test: the event-step optimization must return exactly
        # what a literal scan of all 1001 grid points through negotiate()
        # (rewriting the raw header element) would produce.
        supported = ["en-US", "zh-CN", "zh-TW", "ja-JP"]
        default = "en-US"
        result = analyze_q_stability(
            header=header, supported=supported, default=default
        )
        parsed = parse_accept_language(header)
        elements = header.split(",")
        base_key = (
            result["baseline"]["selected_language"],
            result["baseline"]["fallback"],
        )

        for entry, report in zip(parsed, result["ranges"]):
            outcomes: dict[int, tuple[str, bool]] = {}

            def brute_outcome(step: int) -> tuple[str, bool]:
                if step not in outcomes:
                    token = f"{step // 1000}.{step % 1000:03d}"
                    parts = elements[entry.position - 1].split(";")
                    replaced = False
                    for idx in range(1, len(parts)):
                        if (
                            parts[idx].strip().split("=", 1)[0].strip()
                            == "q"
                        ):
                            head, _sep, _value = parts[idx].partition("=")
                            parts[idx] = head + "=" + token
                            replaced = True
                    if not replaced:
                        parts.append(f"q={token}")
                    rewritten = ",".join(
                        elements[: entry.position - 1]
                        + [";".join(parts)]
                        + elements[entry.position :]
                    )
                    outcome = negotiate(rewritten, supported, default)
                    outcomes[step] = (
                        outcome["selected_language"],
                        outcome["fallback"],
                    )
                return outcomes[step]

            original_step = round(entry.q * 1000)
            lower = original_step
            while lower - 1 >= 0 and brute_outcome(lower - 1) == base_key:
                lower -= 1
            upper = original_step
            while upper + 1 <= 1000 and brute_outcome(upper + 1) == base_key:
                upper += 1

            assert report["lower_bound"] == lower / 1000
            assert report["upper_bound"] == upper / 1000

            for side_step, actual in (
                (lower - 1, report["lower_outside"]),
                (upper + 1, report["upper_outside"]),
            ):
                if side_step < 0 or side_step > 1000:
                    assert actual is None
                else:
                    selected, fell_back = brute_outcome(side_step)
                    assert actual == {
                        "q": side_step / 1000,
                        "selected_language": selected,
                        "fallback": fell_back,
                    }
                    # The reported outside point must differ from baseline.
                    assert (selected, fell_back) != base_key
