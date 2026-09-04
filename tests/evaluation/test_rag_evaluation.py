"""用可手算样本保护评测口径，避免空结果和去重虚增指标。"""

import pytest

from scripts.evaluate_conversation_rag import evaluate, select_results, tune_threshold


def test_precision_is_micro_average_and_no_answer_is_separate():
    """将微平均误写成宏平均、或把空结果当正确返回时应失败。"""
    labels = {"a": [1, 2, 3], "b": [4], "c": [5, 6], "d": []}
    results = {"a": [1, 2, 3], "b": [9], "c": [5, 8], "d": []}
    metrics = evaluate(labels, results)
    assert metrics["precision_micro"] == pytest.approx(4 / 6)
    assert metrics["hit"] == pytest.approx(2 / 3)
    assert metrics["recall"] == pytest.approx(0.5)
    assert metrics["no_answer_false_positive_rate"] == 0
    assert metrics["positive_count"] == 3
    assert metrics["negative_count"] == 1


def test_all_empty_precision_is_undefined():
    metrics = evaluate({"a": [1], "b": []}, {"a": [], "b": []})
    assert metrics["precision_micro"] is None
    assert metrics["hit"] == 0
    assert metrics["no_answer_false_positive_rate"] == 0


def test_threshold_is_inclusive_and_dedup_precedes_top_k():
    """低分重复项不能挤掉 Top-K 内的独立证据。"""
    candidates = [
        {"id": 1, "score": 0.9},
        {"id": 2, "score": 0.8},
        {"id": 3, "score": 0.5},
        {"id": 4, "score": 0.49},
    ]
    vectors = {1: [1.0, 0.0], 2: [1.0, 0.0], 3: [0.0, 1.0], 4: [-1.0, 0.0]}
    assert select_results(candidates, vectors, 0.5, None, limit=2) == [1, 2]
    assert select_results(candidates, vectors, 0.5, 0.98, limit=2) == [1, 3]


def test_threshold_selection_uses_calibration_only():
    """调参函数仅接受传入的校准样本，且不能靠清空所有结果获胜。"""
    labels = {"positive": [1], "negative": []}
    scored = {"positive": [{"id": 1, "score": 0.8}], "negative": [{"id": 2, "score": 0.3}]}
    threshold, feasible, _ = tune_threshold(labels, scored)
    assert 0.3 < threshold <= 0.8
    assert feasible
