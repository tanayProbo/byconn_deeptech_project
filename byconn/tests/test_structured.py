"""Tests for schema-driven extraction: validation, citations, merging."""

import json

import pytest

from byconn.pipeline import structured
from byconn.pipeline.llm_extractor import LLMExtractor
from byconn.tests.doubles import run_async

PAGE = """# Pricing

**Starter** costs $19 per month and includes 3 seats.

The [Pro plan](https://x.test/pro) costs $49 per month – unlimited seats.
"""

PLANS_SCHEMA = {
    "type": "object",
    "properties": {
        "plans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
                "required": ["name", "price"],
            },
        }
    },
    "required": ["plans"],
}


class TestValidateSchema:
    def test_accepts_an_object_schema(self):
        assert structured.validate_schema(PLANS_SCHEMA) is PLANS_SCHEMA

    @pytest.mark.parametrize("bad", [
        [], "x", {"type": "array"}, {"type": "object", "properties": 5},
    ])
    def test_rejects_invalid_schemas(self, bad):
        with pytest.raises(ValueError):
            structured.validate_schema(bad)

    def test_rejects_oversized_schema(self):
        big = {"type": "object", "description": "x" * (structured.MAX_SCHEMA_BYTES + 1)}
        with pytest.raises(ValueError):
            structured.validate_schema(big)


class TestPointers:
    def test_leaf_pointers_skip_nulls_and_escape_keys(self):
        data = {"a/b": 1, "list": [None, {"x": "y"}], "empty": ""}
        assert structured.leaf_pointers(data) == [("/a~1b", 1), ("/list/1/x", "y")]

    @pytest.mark.parametrize("raw, expected", [
        ("/plans/0/price", "/plans/0/price"),
        ("plans.0.price", "/plans/0/price"),
        ("plans[0].price", "/plans/0/price"),
        ("/data/plans/0/price", "/plans/0/price"),
        ("data.plans.0.price", "/plans/0/price"),
        ("", None),
        (7, None),
    ])
    def test_normalize_pointer(self, raw, expected):
        assert structured.normalize_pointer(raw) == expected


class TestVerifyCitations:
    def test_real_quote_is_kept_despite_case_whitespace_and_markup(self):
        data = {"plans": [{"name": "Pro", "price": "$49"}]}
        cites = {"/plans/0/price": ["the  PRO plan costs $49 per month - unlimited"]}
        verified, unverified = structured.verify_citations(data, cites, PAGE, "https://x.test")
        assert verified["/plans/0/price"][0]["url"] == "https://x.test"
        assert "/plans/0/price" not in unverified

    def test_fabricated_quote_is_dropped_and_value_flagged(self):
        data = {"plans": [{"name": "Enterprise", "price": "$999"}]}
        cites = {
            "/plans/0/price": ["Enterprise costs $999 per month"],
            "/plans/0/name": ["Enterprise plan for large teams"],
        }
        verified, unverified = structured.verify_citations(data, cites, PAGE, "u")
        assert verified == {}
        assert unverified == ["/plans/0/name", "/plans/0/price"]

    def test_long_value_found_verbatim_is_its_own_evidence(self):
        data = {"plans": [{"name": "Starter", "price": "$19"}]}
        verified, unverified = structured.verify_citations(data, {}, PAGE, "u")
        assert verified["/plans/0/name"][0]["quote"] == "Starter"
        # "$19" is too short to prove anything by itself.
        assert unverified == ["/plans/0/price"]

    def test_citation_shapes_models_produce_are_accepted(self):
        data = {"answer": "3 seats"}
        for cites in ({"answer": "includes 3 seats"},
                      {"/answer": [{"quote": "includes 3 seats"}]}):
            verified, _ = structured.verify_citations(data, cites, PAGE, "u")
            assert verified["/answer"][0]["quote"] == "includes 3 seats"


class TestSplitWindows:
    def test_respects_budget_and_cap(self):
        text = "\n\n".join(f"para {i} " + "word " * 10 for i in range(10))
        windows = structured.split_windows(text, 25, lambda t: len(t.split()), max_windows=3)
        assert len(windows) == 3
        assert all(len(w.split()) <= 25 for w in windows)

    def test_huge_paragraph_is_hard_cut(self):
        windows = structured.split_windows("x" * 100, 5, len, max_windows=99)
        assert "".join(windows) == "x" * 100


class TestMergeStructured:
    def test_arrays_concatenate_without_duplicates_and_citations_rebase(self):
        a = {"data": {"plans": [{"name": "A"}]},
             "citations": {"/plans/0/name": [{"quote": "qa", "url": "u"}]}, "unverified": []}
        b = {"data": {"plans": [{"name": "A"}, {"name": "B"}]},
             "citations": {"/plans/0/name": [{"quote": "qa2", "url": "u"}],
                           "/plans/1/name": [{"quote": "qb", "url": "u"}]},
             "unverified": []}
        merged = structured.merge_structured([a, b])
        assert merged["data"] == {"plans": [{"name": "A"}, {"name": "B"}]}
        assert [q["quote"] for q in merged["citations"]["/plans/0/name"]] == ["qa", "qa2"]
        assert merged["citations"]["/plans/1/name"][0]["quote"] == "qb"

    def test_first_scalar_wins_unless_only_a_later_one_is_verified(self):
        first = {"data": {"ceo": "Guess"}, "citations": {}, "unverified": ["/ceo"]}
        second = {"data": {"ceo": "Ada"},
                  "citations": {"/ceo": [{"quote": "CEO Ada", "url": "u"}]}, "unverified": []}
        merged = structured.merge_structured([first, second])
        assert merged["data"]["ceo"] == "Ada"
        assert merged["unverified"] == []

        kept = structured.merge_structured([second, first])
        assert kept["data"]["ceo"] == "Ada"
        assert "/ceo" in kept["citations"]

    def test_empty_parts_give_empty_result(self):
        assert structured.merge_structured([None, {}])["data"] == {}


class TestExtractStructured:
    GOOD = json.dumps({
        "data": {"plans": [{"name": "Starter", "price": "$19"}, {"name": "Pro", "price": "$49"}]},
        "citations": {
            "/plans/0/price": ["Starter costs $19 per month"],
            "/plans/1/price": ["costs $49 per month"],
            "/plans/1/name": ["Pro plan"],
        },
    })

    def _extractor(self, monkeypatch, replies):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        extractor = LLMExtractor(cache_size=0)
        prompts = []

        async def fake_call(prompt, image=None, model=None):
            prompts.append(prompt)
            return replies[min(len(prompts), len(replies)) - 1]

        monkeypatch.setattr(extractor, "_call_with_retries", fake_call)
        return extractor, prompts

    def test_happy_path_returns_verified_data(self, monkeypatch):
        extractor, prompts = self._extractor(monkeypatch, [self.GOOD])
        result = run_async(extractor.extract_structured(PAGE, "pricing", PLANS_SCHEMA, "u"))
        assert result["data"]["plans"][1] == {"name": "Pro", "price": "$49"}
        assert result["unverified"] == []
        assert result["schema_errors"] == []
        assert "pricing" in prompts[0] and '"plans"' in prompts[0]

    def test_schema_violation_is_retried_once_with_the_error(self, monkeypatch):
        bad = json.dumps({"data": {"plans": [{"name": "Starter"}]}, "citations": {}})
        extractor, prompts = self._extractor(monkeypatch, [bad, self.GOOD])
        result = run_async(extractor.extract_structured(PAGE, "", PLANS_SCHEMA, "u"))
        assert len(prompts) == 2
        assert "'price' is a required property" in prompts[1]
        assert result["schema_errors"] == []

    def test_persistent_violation_is_reported_not_raised(self, monkeypatch):
        bad = json.dumps({"data": {"plans": "none"}, "citations": {}})
        extractor, prompts = self._extractor(monkeypatch, [bad, bad])
        result = run_async(extractor.extract_structured(PAGE, "", PLANS_SCHEMA, "u"))
        assert len(prompts) == 2
        assert result["schema_errors"]

    def test_unusable_reply_gives_empty_result(self, monkeypatch):
        extractor, _ = self._extractor(monkeypatch, ["sorry, I can't"])
        result = run_async(extractor.extract_structured(PAGE, "", PLANS_SCHEMA, "u"))
        assert result["data"] == {} and result["windows"] == 1

    def test_default_schema_for_prompt_only_jobs(self, monkeypatch):
        reply = json.dumps({"data": {"answer": "3 seats"},
                            "citations": {"/answer": ["includes 3 seats"]}})
        extractor, prompts = self._extractor(monkeypatch, [reply])
        result = run_async(extractor.extract_structured(PAGE, "how many seats?", None, "u"))
        assert result["data"]["answer"] == "3 seats"
        assert '"answer"' in prompts[0]

    def test_long_page_is_windowed_and_merged(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_WINDOWS", "3")
        replies = iter([
            json.dumps({"data": {"plans": [{"name": "Starter", "price": "$19"}]}}),
            json.dumps({"data": {"plans": [{"name": "Starter", "price": "$19"},
                                           {"name": "Pro", "price": "$49"}]}}),
            json.dumps({"data": {"plans": []}}),
        ])
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        extractor = LLMExtractor(cache_size=0, max_input_tokens=20)

        async def fake_call(prompt, image=None, model=None):
            return next(replies)

        monkeypatch.setattr(extractor, "_call_with_retries", fake_call)
        page = "\n\n".join(["Starter costs $19 " + "filler " * 12] * 2 + ["Pro costs $49 " + "x " * 12] * 3)
        result = run_async(extractor.extract_structured(page, "", PLANS_SCHEMA, "u"))
        assert result["windows"] == 3 and result["windows_skipped"] == 2
        assert [p["name"] for p in result["data"]["plans"]] == ["Starter", "Pro"]

    def test_no_key_returns_empty_without_calling(self, monkeypatch):
        extractor = LLMExtractor()
        result = run_async(extractor.extract_structured(PAGE, "x", None, "u"))
        assert result["data"] == {} and result["windows"] == 0


class TestRepairLeavesStringsAlone:
    def test_colon_inside_a_value_survives(self):
        raw = "{summary: 'Note, time: 5pm', ok: True,}"
        assert LLMExtractor._loads(raw) == {"summary": "Note, time: 5pm", "ok": True}

    def test_double_quoted_value_with_key_like_text(self):
        raw = '{"summary": "a, b: c", count: 2,}'
        assert LLMExtractor._loads(raw) == {"summary": "a, b: c", "count": 2}
