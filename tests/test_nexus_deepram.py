import pytest

from jarvis_books.nexus.deepram import GB, DeepRAM, ModelShape

# Real Qwen3-4B metadata as Ollama reports it (36 layers, 8 key/value heads of 128, 262k context) and its file size.
QWEN_SHOW = {"model_info": {"general.architecture": "qwen3", "general.parameter_count": 4022468096,
                            "qwen3.block_count": 36, "qwen3.attention.head_count": 32,
                            "qwen3.attention.head_count_kv": 8, "qwen3.attention.key_length": 128,
                            "qwen3.attention.value_length": 128, "qwen3.embedding_length": 2560,
                            "qwen3.context_length": 262144},
             "details": {"family": "qwen3", "quantization_level": "Q4_K_M"}}
QWEN_BYTES = 2_497_281_120


def qwen():
    return ModelShape.from_ollama("qwen3:4b", QWEN_SHOW, QWEN_BYTES)


def test_shape_is_read_from_the_models_own_metadata():
    s = qwen()
    assert (s.layers, s.kv_heads, s.key_length, s.max_context) == (36, 8, 128, 262144)
    assert s.parameters == 4022468096 and s.quantization == "Q4_K_M"
    # 36 layers x 8 heads x (128 + 128) values x 2 bytes = 147,456 bytes per token
    assert s.kv_bytes_per_token() == 147456


def test_plan_adds_weights_cache_overhead_and_keeps_a_margin():
    plan = DeepRAM().plan(qwen(), 8192, available_bytes=int(9 * GB), total_bytes=int(16 * GB))
    assert plan.fits and plan.weights_gb == pytest.approx(2.326, abs=0.01)
    assert plan.cache_gb == pytest.approx(147456 * 8192 / GB, abs=0.001)  # 1.125 GB
    assert plan.margin_gb == 1.6  # 10% of 16 GB beats the 1.5 GB floor
    assert plan.total_gb == pytest.approx(plan.weights_gb + plan.cache_gb + plan.overhead_gb + plan.scratch_gb,
                                          abs=0.002)


def test_a_model_that_would_freeze_the_computer_is_refused_with_options():
    ram = DeepRAM()
    tight = ram.plan(qwen(), 131072, available_bytes=int(6 * GB), total_bytes=int(16 * GB))
    assert not tight.fits and "only 6.0 GB is free" in tight.reason
    best = ram.largest_context(qwen(), int(6 * GB), int(16 * GB))
    assert best is not None and 2048 <= best < 131072
    assert ram.plan(qwen(), best, int(6 * GB), int(16 * GB)).fits
    assert not ram.plan(qwen(), best + 1024, int(6 * GB), int(16 * GB)).fits
    assert ram.largest_context(qwen(), int(2 * GB), int(16 * GB)) is None


def test_loaded_models_only_need_their_context_and_unknown_free_memory_is_not_safe():
    ram = DeepRAM()
    fresh = ram.plan(qwen(), 4096, int(4 * GB), int(16 * GB))
    loaded = ram.plan(qwen(), 4096, int(4 * GB), int(16 * GB), already_loaded=True)
    assert not fresh.fits and loaded.fits and loaded.weights_gb == 0
    assert not ram.plan(qwen(), 4096, None).fits


def test_missing_layer_data_is_planned_pessimistically():
    blind = ModelShape("mystery", int(1 * GB))
    plan = DeepRAM().plan(blind, 4096, int(3 * GB), int(16 * GB))
    assert not plan.known and plan.cache_gb is None and "assumed" in plan.reason
    assert DeepRAM().plan(blind, 4096, int(8 * GB), int(16 * GB)).fits


def test_choose_prefers_the_largest_model_that_fits_and_points_to_alternatives():
    small = ModelShape.from_ollama("lfm:1.7b", {"model_info": {"general.architecture": "lfm2",
                                                               "lfm2.block_count": 16,
                                                               "lfm2.attention.head_count": 32,
                                                               "lfm2.attention.head_count_kv": 8,
                                                               "lfm2.embedding_length": 2048,
                                                               "lfm2.context_length": 128000}}, int(1.2 * GB))
    plans = DeepRAM().choose([small, qwen()], 8192, int(4.5 * GB), int(16 * GB))
    assert [p.model for p in plans] == ["lfm:1.7b", "qwen3:4b"] and plans[0].fits and not plans[1].fits
    assert plans[1].alternatives == ["lfm:1.7b"]
    none_fit = DeepRAM().choose([qwen()], 8192, int(1 * GB), int(16 * GB))
    assert "another computer in the Collective" in none_fit[0].reason
    with pytest.raises(ValueError):
        DeepRAM(cache_type="f64")
