"""Tests for byconn.pipeline: cleaning, embedding, and LLM extraction."""

import asyncio

import pytest

from byconn.pipeline.cleaning import DataCleaner
from byconn.pipeline.embedder import DEFAULT_DIMENSIONS, DocumentEmbedder
from byconn.pipeline.llm_extractor import LLMExtractor, is_permanent_error

# empty_result() is a static method on LLMExtractor; alias for brevity.
empty_result = LLMExtractor.empty_result

from byconn.tests.doubles import run_async

SAMPLE_HTML = (
    "<html><head><style>.a{color:red}</style><script>evil=1</script></head>"
    "<body><nav>NAVIGATION</nav><header>HEADER</header>"
    "<h1>Title</h1><p>Body paragraph.</p>"
    "<ul><li>one</li><li>two</li></ul>"
    '<a href="https://x.test">link text</a>'
    "<footer>FOOTER</footer></body></html>"
)


# ==========================================================================
# DataCleaner
# ==========================================================================
class TestDataCleaner:
    def test_strips_boilerplate_tags(self):
        cleaned = DataCleaner().clean_html(SAMPLE_HTML)
        for tag in ("script", "style", "nav", "header", "footer"):
            assert f"<{tag}" not in cleaned, f"{tag} should be removed"

    def test_keeps_content_tags(self):
        cleaned = DataCleaner().clean_html(SAMPLE_HTML)
        assert "<h1>Title</h1>" in cleaned
        assert "Body paragraph." in cleaned

    def test_html_to_markdown_emits_headings_and_lists(self):
        markdown = DataCleaner().html_to_markdown(SAMPLE_HTML)
        assert "# Title" in markdown
        assert "Body paragraph." in markdown
        assert "one" in markdown and "two" in markdown

    def test_html_to_markdown_emits_links(self):
        markdown = DataCleaner().html_to_markdown(SAMPLE_HTML)
        assert "[link text](https://x.test)" in markdown

    def test_html_to_markdown_collapses_blank_runs(self):
        cleaned = DataCleaner().html_to_markdown(SAMPLE_HTML)
        assert "\n\n\n" not in cleaned

    def test_table_is_rendered_as_a_pipe_table(self):
        """Regression: tables were previously dropped entirely."""
        html = (
            "<table><thead><tr><th>Region</th><th>Revenue</th></tr></thead>"
            "<tbody><tr><td>EMEA</td><td>1200</td></tr>"
            "<tr><td>APAC</td><td>980</td></tr></tbody></table>"
        )
        markdown = DataCleaner().html_to_markdown(html)
        assert "| Region | Revenue |" in markdown
        assert "| --- | --- |" in markdown
        assert "| EMEA | 1200 |" in markdown
        assert "| APAC | 980 |" in markdown

    def test_table_cells_are_preserved_whole(self):
        """Regression: a cell must not be shredded into single characters."""
        markdown = DataCleaner().html_to_markdown(
            "<table><tr><td>Revenue</td><td>1,200</td></tr></table>"
        )
        assert "Revenue" in markdown
        assert "1,200" in markdown
        for line in markdown.splitlines():
            if line.startswith("|"):
                assert line.count("|") == 3, f"malformed row: {line!r}"

    def test_headerless_table_stays_well_formed(self):
        markdown = DataCleaner().html_to_markdown(
            "<table><tr><td>alpha</td><td>beta</td></tr></table>"
        )
        assert markdown.count("| --- | --- |") == 1
        assert "alpha" in markdown and "beta" in markdown

    def test_ragged_rows_are_padded(self):
        markdown = DataCleaner().html_to_markdown(
            "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td></tr></table>"
        )
        for line in markdown.splitlines():
            if line.startswith("|"):
                assert line.count("|") == 4, f"unpadded row: {line!r}"

    def test_pipes_in_cells_are_escaped(self):
        markdown = DataCleaner().html_to_markdown(
            "<table><tr><th>a|b</th><th>c</th></tr></table>"
        )
        assert r"a\|b" in markdown

    def test_unordered_list_items_are_separated(self):
        """Regression: items previously collapsed onto one line ('- i1- i2')."""
        markdown = DataCleaner().html_to_markdown("<ul><li>one</li><li>two</li></ul>")
        lines = [l for l in markdown.splitlines() if l.strip()]
        assert lines == ["- one", "- two"]

    def test_ordered_list_numbers_sequentially(self):
        markdown = DataCleaner().html_to_markdown(
            "<ol><li>first</li><li>second</li><li>third</li></ol>"
        )
        lines = [l for l in markdown.splitlines() if l.strip()]
        assert lines == ["1. first", "2. second", "3. third"]

    def test_nested_lists_are_indented(self):
        markdown = DataCleaner().html_to_markdown(
            "<ul><li>parent<ul><li>child</li></ul></li></ul>"
        )
        assert "- parent" in markdown
        assert "  - child" in markdown

    def test_standalone_link_keeps_its_href(self):
        """Regression: a bare <a> at block level lost its URL."""
        markdown = DataCleaner().html_to_markdown('<a href="https://x.test">label</a>')
        assert "[label](https://x.test)" in markdown

    def test_inline_formatting_inside_paragraph(self):
        markdown = DataCleaner().html_to_markdown(
            "<p>Grew <strong>12%</strong> in <em>Q3</em> see "
            '<a href="https://x.test/r">here</a></p>'
        )
        assert "**12%**" in markdown
        assert "*Q3*" in markdown
        assert "[here](https://x.test/r)" in markdown

    def test_code_block_is_fenced(self):
        markdown = DataCleaner().html_to_markdown("<pre>pip install byconn-x</pre>")
        assert "```" in markdown
        assert "pip install byconn-x" in markdown

    def test_blockquote_is_prefixed(self):
        markdown = DataCleaner().html_to_markdown("<blockquote>Revenue is up.</blockquote>")
        assert "> Revenue is up." in markdown

    def test_loose_text_in_a_container_is_not_lost(self):
        markdown = DataCleaner().html_to_markdown("<div>Bare text here.</div>")
        assert "Bare text here." in markdown

    def test_deeper_headings_are_supported(self):
        markdown = DataCleaner().html_to_markdown("<h4>Deep</h4>")
        assert "#### Deep" in markdown

    def test_empty_input_is_safe(self):
        cleaner = DataCleaner()
        assert cleaner.html_to_markdown("") == ""
        assert cleaner.html_to_markdown("<body></body>") == ""

    def test_shingles_and_jaccard_detect_duplicates(self):
        cleaner = DataCleaner()
        left = cleaner.get_shingle_hash("the quick brown fox jumps over the lazy dog")
        right = cleaner.get_shingle_hash("the quick brown fox jumps over the lazy dog")
        assert left == right
        assert cleaner.compute_jaccard_similarity(
            "the quick brown fox jumps over the lazy dog",
            "the quick brown fox jumps over the lazy dog",
        ) == 1.0

    def test_jaccard_of_unrelated_text_is_low(self):
        cleaner = DataCleaner()
        score = cleaner.compute_jaccard_similarity(
            "alpha beta gamma delta epsilon", "zulu yankee xray whiskey foxtrot"
        )
        assert score < 0.2

    def test_empty_input_yields_zero_similarity(self):
        assert DataCleaner().compute_jaccard_similarity("", "") == 0.0


# ==========================================================================
# DocumentEmbedder - chunking
# ==========================================================================
class TestChunking:
    def test_short_text_is_one_chunk(self):
        assert DocumentEmbedder().split_into_chunks("one two three") == ["one two three"]

    def test_empty_text_yields_no_chunks(self):
        assert DocumentEmbedder().split_into_chunks("") == []
        assert DocumentEmbedder().split_into_chunks(None) == []

    def test_chunks_overlap(self):
        embedder = DocumentEmbedder(chunk_size=10, chunk_overlap=5)
        chunks = embedder.split_into_chunks(" ".join(str(i) for i in range(40)))
        assert len(chunks) > 1
        # A 5-word stride over a 10-word window must repeat content.
        assert set(chunks[0].split()) & set(chunks[1].split())

    def test_zero_overlap_does_not_loop_forever(self):
        embedder = DocumentEmbedder(chunk_size=10, chunk_overlap=10)
        chunks = embedder.split_into_chunks(" ".join(str(i) for i in range(30)))
        assert len(chunks) == 3


# ==========================================================================
# DocumentEmbedder - provider resolution
# ==========================================================================
class TestEmbedderProviderResolution:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for key in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS",
                    "OPENAI_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    def test_explicit_provider_is_respected(self):
        assert DocumentEmbedder(provider="openai").provider == "openai"

    def test_model_defaults_per_provider(self):
        assert DocumentEmbedder(provider="openai").model == "text-embedding-3-small"
        assert DocumentEmbedder(provider="sentence-transformers").model == "all-MiniLM-L6-v2"

    def test_model_env_override(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_MODEL", "custom-model")
        assert DocumentEmbedder(provider="openai").model == "custom-model"

    def test_dimensions_default_to_qdrant_width(self):
        assert DocumentEmbedder().dimensions == DEFAULT_DIMENSIONS == 384

    def test_dimensions_env_override(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1536")
        assert DocumentEmbedder().dimensions == 1536

    def test_bad_dimensions_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "not-a-number")
        assert DocumentEmbedder().dimensions == 384

    def test_injected_client_selects_openai(self):
        assert DocumentEmbedder(embedding_client=object()).provider == "openai"


# ==========================================================================
# DocumentEmbedder - dense embeddings
# ==========================================================================
class _FakeEmbeddingItem:
    def __init__(self, embedding):
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, vectors):
        self.data = [_FakeEmbeddingItem(v) for v in vectors]


class _FakeEmbeddingsEndpoint:
    """Captures the kwargs sent to the OpenAI embeddings endpoint."""

    def __init__(self, dimensions=384, fail_times=0):
        self.calls = []
        self.dimensions = dimensions
        self.fail_times = fail_times

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("upstream unavailable")
        count = len(kwargs["input"])
        return _FakeEmbeddingResponse([[0.1] * self.dimensions for _ in range(count)])


class _FakeOpenAI:
    def __init__(self, endpoint):
        self.embeddings = endpoint


class TestDenseEmbeddings:
    def test_no_provider_returns_empty_not_fake_vectors(self):
        """The critical regression: never fabricate vectors when unavailable."""
        embedder = DocumentEmbedder(provider="none")
        assert embedder.is_available is False
        result = run_async(embedder.generate_dense_embeddings(["a", "b"]))
        assert result == []

    def test_empty_input_short_circuits(self):
        assert run_async(DocumentEmbedder(provider="openai").generate_dense_embeddings([])) == []

    def test_openai_round_trip(self):
        endpoint = _FakeEmbeddingsEndpoint()
        embedder = DocumentEmbedder(provider="openai", embedding_client=_FakeOpenAI(endpoint))
        result = run_async(embedder.generate_dense_embeddings(["alpha", "beta"]))
        assert len(result) == 2
        assert all(len(vector) == 384 for vector in result)

    def test_openai_requests_truncated_dimensions(self):
        endpoint = _FakeEmbeddingsEndpoint()
        embedder = DocumentEmbedder(provider="openai", embedding_client=_FakeOpenAI(endpoint))
        run_async(embedder.generate_dense_embeddings(["alpha"]))
        assert endpoint.calls[0]["dimensions"] == 384
        assert endpoint.calls[0]["model"] == "text-embedding-3-small"

    def test_dimensions_omitted_for_legacy_models(self):
        endpoint = _FakeEmbeddingsEndpoint()
        embedder = DocumentEmbedder(provider="openai", model="text-embedding-ada-002",
                                    embedding_client=_FakeOpenAI(endpoint))
        run_async(embedder.generate_dense_embeddings(["alpha"]))
        assert "dimensions" not in endpoint.calls[0]

    def test_batching_splits_large_inputs(self):
        endpoint = _FakeEmbeddingsEndpoint()
        embedder = DocumentEmbedder(provider="openai", batch_size=2,
                                    embedding_client=_FakeOpenAI(endpoint))
        result = run_async(embedder.generate_dense_embeddings(["a", "b", "c", "d", "e"]))
        assert len(endpoint.calls) == 3
        assert len(result) == 5

    def test_provider_failure_returns_empty(self):
        endpoint = _FakeEmbeddingsEndpoint(fail_times=99)
        embedder = DocumentEmbedder(provider="openai",
                                    embedding_client=_FakeOpenAI(endpoint))
        assert run_async(embedder.generate_dense_embeddings(["a"])) == []

    def test_wrong_dimensionality_is_rejected(self):
        endpoint = _FakeEmbeddingsEndpoint(dimensions=1536)
        embedder = DocumentEmbedder(provider="openai", embedding_client=_FakeOpenAI(endpoint))
        # Guards against writing vectors that Qdrant's 384-dim collection rejects.
        assert run_async(embedder.generate_dense_embeddings(["a"])) == []

    def test_local_provider_uses_the_model(self, monkeypatch):
        calls = {}

        class _FakeModel:
            def encode(self, texts, **kwargs):
                calls["texts"] = list(texts)
                return [[0.2] * 384 for _ in texts]

        embedder = DocumentEmbedder(provider="sentence-transformers")
        embedder._local_model = _FakeModel()
        result = run_async(embedder.generate_dense_embeddings(["one", "two"]))
        assert calls["texts"] == ["one", "two"]
        assert len(result) == 2 and len(result[0]) == 384

    def test_sparse_tokens(self):
        tokens = DocumentEmbedder().generate_sparse_tokens("a b a c")
        assert tokens["a"] == pytest.approx(0.5)
        assert tokens["b"] == pytest.approx(0.25)

    def test_sparse_tokens_empty(self):
        assert DocumentEmbedder().generate_sparse_tokens("") == {}


# ==========================================================================
# LLMExtractor
# ==========================================================================
@pytest.fixture(autouse=True)
def _llm_env(monkeypatch):
    for key in ("LLM_PROVIDER", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "OPENAI_MODEL", "GEMINI_MODEL", "LLM_MAX_RETRIES", "LLM_MAX_INPUT_TOKENS"):
        monkeypatch.delenv(key, raising=False)


class TestLLMProviderResolution:
    def test_no_key_reports_unavailable(self):
        extractor = LLMExtractor()
        assert extractor.is_available is False
        assert extractor.provider == "openai"

    def test_openai_key_is_detected(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        assert LLMExtractor().provider == "openai"
        assert LLMExtractor().is_available is True

    def test_gemini_key_is_detected(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        extractor = LLMExtractor()
        assert extractor.provider == "gemini"
        assert extractor.model == "gemini-3.8-flash"
        assert extractor.is_available is True

    def test_openai_wins_when_both_keys_present(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert LLMExtractor().provider == "openai"

    def test_explicit_provider_env_is_honoured(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert LLMExtractor().provider == "gemini"

    def test_provider_arg_beats_env(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        assert LLMExtractor(provider="openai").provider == "openai"

    def test_api_key_arg_makes_it_available(self):
        assert LLMExtractor(provider="openai", api_key="injected").is_available is True

    def test_bad_retry_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_RETRIES", "abc")
        assert LLMExtractor().max_retries == 2


class TestLLMResponseParsing:
    def test_parses_plain_json(self):
        result = LLMExtractor().parse_response(
            '{"entities":[{"name":"A","type":"PERSON"}],'
            '"triples":[{"subject":"A","predicate":"KNOWS","object":"B"}],'
            '"topics":["t"],"summary":"s"}'
        )
        assert result["entities"] == [{"name": "A", "type": "PERSON"}]
        assert result["triples"][0]["predicate"] == "KNOWS"

    def test_strips_markdown_fence(self):
        result = LLMExtractor().parse_response('```json\n{"entities":[{"name":"A"}]}\n```')
        assert len(result["entities"]) == 1

    def test_recovers_json_from_surrounding_prose(self):
        result = LLMExtractor().parse_response(
            'Sure! Here you go:\n{"entities":[{"name":"Z"}]}\nHope that helps.'
        )
        assert len(result["entities"]) == 1

    def test_unparseable_returns_empty(self):
        assert LLMExtractor().parse_response("no json here") == empty_result()

    def test_empty_string_returns_empty(self):
        assert LLMExtractor().parse_response("") == empty_result()

    def test_non_object_json_returns_empty(self):
        assert LLMExtractor().parse_response("[1, 2, 3]") == empty_result()

    def test_drops_and_dedupes_entities(self):
        result = LLMExtractor().parse_response(
            '{"entities":[{"name":"A"},{"name":"a"},{"name":"  "},{"x":1},"str",null]}'
        )
        assert [e["name"] for e in result["entities"]] == ["A"]

    def test_unknown_entity_type_is_coerced(self):
        result = LLMExtractor().parse_response('{"entities":[{"name":"X","type":"WIDGET"}]}')
        assert result["entities"][0]["type"] == "ENTITY"

    def test_known_entity_type_is_uppercased(self):
        result = LLMExtractor().parse_response('{"entities":[{"name":"X","type":"person"}]}')
        assert result["entities"][0]["type"] == "PERSON"

    def test_drops_incomplete_triples(self):
        result = LLMExtractor().parse_response(
            '{"triples":[{"subject":"A","predicate":"P","object":"B"},'
            '{"subject":"","predicate":"P","object":"B"},{"subject":"A"}]}'
        )
        assert len(result["triples"]) == 1

    def test_dedupes_triples(self):
        result = LLMExtractor().parse_response(
            '{"triples":[{"subject":"A","predicate":"P","object":"B"},'
            '{"subject":"a","predicate":"p","object":"b"}]}'
        )
        assert len(result["triples"]) == 1

    def test_respects_entity_and_triple_caps(self):
        extractor = LLMExtractor(max_entities=2, max_triples=2)
        result = extractor.parse_response(
            '{"entities":[{"name":"1"},{"name":"2"},{"name":"3"},{"name":"4"}],'
            '"triples":[{"subject":"1","predicate":"p","object":"2"},'
            '{"subject":"3","predicate":"p","object":"4"},'
            '{"subject":"5","predicate":"p","object":"6"}]}'
        )
        assert len(result["entities"]) == 2
        assert len(result["triples"]) == 2

    def test_topics_filtered_and_summary_coerced(self):
        result = LLMExtractor().parse_response('{"topics":["a","  ","b"],"summary":123}')
        assert result["topics"] == ["a", "b"]
        assert result["summary"] == "123"


class TestLLMTokenBudget:
    def test_counts_tokens(self):
        assert LLMExtractor().count_tokens("<p>hello world</p>") > 0

    def test_short_text_untouched(self):
        extractor = LLMExtractor()
        text = "<p>hello</p>"
        assert extractor.truncate_to_budget(text) == text

    def test_long_text_truncated_and_marked(self):
        extractor = LLMExtractor(max_input_tokens=50)
        result = extractor.truncate_to_budget("word " * 5000)
        assert "[...truncated...]" in result
        assert len(result) < 5000 * 6

    def test_empty_text_handled(self):
        assert LLMExtractor().truncate_to_budget("") == ""


class TestLLMCallPath:
    class _NotFound(Exception):
        status_code = 404

    class _RateLimit(Exception):
        status_code = 429

    def test_no_key_returns_empty_without_calling(self):
        extractor = LLMExtractor()
        assert run_async(extractor.extract_knowledge("<html>real text</html>")) == empty_result()

    def test_blank_text_returns_empty(self):
        assert run_async(LLMExtractor().extract_knowledge("   ")) == empty_result()

    def test_successful_call(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        extractor = LLMExtractor(provider="openai", max_retries=0)
        captured = {}

        async def fake_call(prompt, image=None):
            captured["prompt"] = prompt
            return '{"entities":[{"name":"Acme","type":"ORGANIZATION"}],"triples":[]}'

        extractor._call_openai = fake_call
        result = run_async(extractor.extract_knowledge("Acme builds widgets."))
        assert result["entities"] == [{"name": "Acme", "type": "ORGANIZATION"}]
        assert "Acme builds widgets." in captured["prompt"]

    def test_retries_transient_errors_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
        extractor = LLMExtractor(provider="openai", max_retries=3)
        attempts = {"n": 0}

        async def flaky(prompt, image=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise self._RateLimit("slow down")
            return '{"entities":[{"name":"A"}],"triples":[]}'

        extractor._call_openai = flaky
        result = run_async(extractor.extract_knowledge("text"))
        assert attempts["n"] == 3
        assert len(result["entities"]) == 1

    def test_permanent_error_is_not_retried(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        extractor = LLMExtractor(provider="openai", max_retries=5)
        attempts = {"n": 0}

        async def not_found(prompt, image=None):
            attempts["n"] += 1
            raise self._NotFound("model not found")

        extractor._call_openai = not_found
        assert run_async(extractor.extract_knowledge("text")) == empty_result()
        assert attempts["n"] == 1, "a 404 must not burn the retry budget"

    def test_exhausted_retries_return_empty(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
        extractor = LLMExtractor(provider="openai", max_retries=1)

        async def always_fail(prompt, image=None):
            raise RuntimeError("boom")

        extractor._call_openai = always_fail
        assert run_async(extractor.extract_knowledge("text")) == empty_result()


class TestPermanentErrorDetection:
    def test_4xx_status_codes_are_permanent(self):
        for code in (400, 401, 403, 404, 405, 422):
            error = type("E", (Exception,), {"status_code": code})()
            assert is_permanent_error(error) is True

    def test_rate_limit_is_retryable(self):
        error = type("E", (Exception,), {"status_code": 429})()
        assert is_permanent_error(error) is False

    def test_by_name(self):
        assert is_permanent_error(type("NotFoundError", (Exception,), {})()) is True
        assert is_permanent_error(ValueError()) is False
