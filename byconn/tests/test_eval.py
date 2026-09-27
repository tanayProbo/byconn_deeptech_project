"""Tests for the extraction eval: scorer, fixtures and runner plumbing."""

import json

import pytest

from byconn.eval import runner, scoring
from byconn.pipeline import structured
from byconn.pipeline.cleaning import DataCleaner
from byconn.tests.doubles import run_async

FIXTURES = runner.load_fixtures()


class TestNormalizeAndMatch:
    @pytest.mark.parametrize("predicted, gold", [
        ("£51.77", "£51.77"), ("51.77", "£51.77"), ("£1249", "£1,249.00"),
        ("$12 per user / month", "$12"),
        ("A Light in the …", "A Light in the ..."), ("A Light in the", "A Light in the ..."),
        ("“Quoted text”", "Quoted text"), ("  Mixed   CASE ", "mixed case"),
        ("Remote (EU)", "Remote (EU)"),
    ])
    def test_matches(self, predicted, gold):
        assert scoring.values_match(predicted, gold)

    @pytest.mark.parametrize("predicted, gold", [
        ("£51.70", "£51.77"), ("", "Starter"), (None, "$0"),
        ("2027-03-06", "6 March 2027"), ("Berlin", "Berlin, Germany"),
    ])
    def test_mismatches(self, predicted, gold):
        assert not scoring.values_match(predicted, gold)

    def test_percentile_is_nearest_rank(self):
        assert scoring.percentile([4, 1, 3, 2], 50) == 2
        assert scoring.percentile([1, 2], 50) == 1
        assert scoring.percentile([5], 95) == 5
        assert scoring.percentile([], 50) is None


class TestScoreRecords:
    GOLD = [{"name": "A", "price": "$1"}, {"name": "B", "price": "$2"}]

    def _score(self, predicted):
        return scoring.score_records({"items": predicted}, self.GOLD, "items", "name", ["name", "price"])

    def test_perfect(self):
        assert self._score(self.GOLD)["f1"] == 1.0

    def test_wrong_value_is_both_fp_and_fn(self):
        score = self._score([{"name": "A", "price": "$9"}, {"name": "B", "price": "$2"}])
        assert (score["tp"], score["fp"], score["fn"]) == (3, 1, 1)

    def test_invented_record_costs_precision_only(self):
        score = self._score(self.GOLD + [{"name": "Z", "price": "$5"}])
        assert score["recall"] == 1.0 and score["fp"] == 2 and score["invented_records"] == 1

    def test_missing_record_costs_recall(self):
        score = self._score(self.GOLD[:1])
        assert score["precision"] == 1.0 and score["fn"] == 2

    def test_wrong_shape_scores_zero(self):
        assert scoring.score_records({"items": "none"}, self.GOLD, "items", "name", ["name"])["f1"] == 0.0
        assert scoring.score_records(None, self.GOLD, "items", "name", ["name"])["f1"] == 0.0


class TestFixtures:
    def test_there_are_twenty(self):
        assert len(FIXTURES) == 20

    @pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
    def test_fixture_is_well_formed_and_fair(self, fixture):
        task, gold = fixture["task"], fixture["gold"]
        structured.validate_schema(task["schema"])
        assert gold and all(set(record) == set(task["fields"]) for record in gold)
        # Fair: every gold value is visible in the cleaned text the model gets.
        visible = scoring.normalize(DataCleaner().html_to_markdown(fixture["html"]))
        for record in gold:
            for value in record.values():
                assert scoring.normalize(value) in visible, (fixture["name"], value)

    @pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
    def test_gold_scores_perfectly_against_itself(self, fixture):
        task = fixture["task"]
        score = scoring.score_records({task["records_field"]: fixture["gold"]}, fixture["gold"],
                                      task["records_field"], task["key"], task["fields"])
        assert score["f1"] == 1.0


class _GoldExtractor:
    """Answers each fixture with its gold records (runner plumbing only)."""

    def __init__(self, fixture):
        self.fixture = fixture

    async def extract_structured(self, text, instruction, schema, url):
        task = self.fixture["task"]
        data = {task["records_field"]: self.fixture["gold"]}
        citations, unverified = structured.verify_citations(data, {}, text, url)
        return {"data": data, "citations": citations, "unverified": unverified,
                "schema_errors": [], "windows": 1}

    def count_tokens(self, text):
        return len(text.split())


class TestRunner:
    def test_runner_scores_and_aggregates(self):
        rows = [run_async(runner.run_fixture(_GoldExtractor(f), f, timeout=5)) for f in FIXTURES[:3]]
        summary = runner.aggregate(rows)
        assert summary["f1"] == 1.0 and summary["schema_valid_rate"] == 1.0
        assert summary["fixtures"] == 3 and summary["errors"] == 0

    def test_markdown_report(self):
        rows = [run_async(runner.run_fixture(_GoldExtractor(FIXTURES[0]), FIXTURES[0], timeout=5))]
        report = {"run_at": "2026-01-01T00:00:00+00:00", "summary": runner.aggregate(rows), "fixtures": rows,
                  "config": {"model": "m", "provider": "p", "endpoint": "local",
                             "max_input_tokens": 6000, "max_windows": 4}}
        text = runner.to_markdown(report)
        assert "| Field F1 (micro) | 100.0% |" in text and FIXTURES[0]["name"] in text

    def test_cli_refuses_to_run_without_a_model(self, tmp_path, monkeypatch):
        from byconn.eval import __main__ as cli

        monkeypatch.setattr("sys.argv", ["byconn.eval", "--out", str(tmp_path)])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()
        assert exit_info.value.code == 2
        assert list(tmp_path.iterdir()) == []
