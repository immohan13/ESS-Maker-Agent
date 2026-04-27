"""Smoke tests that don't require live AOAI / DirectLine."""
from ess_maker.analyzers.failure_classifier import primary_fix_for
from ess_maker.eval.runner import load_scenarios
from ess_maker.mcs.topics_io import apply_patch
from pathlib import Path


def test_classifier():
    assert primary_fix_for("knowledge_miss") == "knowledge_description"
    assert primary_fix_for("none") == "none"


def test_load_example_scenarios():
    p = Path(__file__).resolve().parents[1] / "prompts" / "scenarios.example.yaml"
    scenarios = load_scenarios(p)
    assert len(scenarios) >= 5
    assert all(s.id and s.prompt for s in scenarios)


def test_apply_topic_patch_minimal():
    topic = {"name": "X", "description": "old"}
    patched = apply_patch(topic, {"new_description": "new", "new_trigger_phrases": ["a", "b"]})
    assert patched["description"] == "new"
    assert patched["trigger"]["intent"]["examples"][0]["text"] == "a"
