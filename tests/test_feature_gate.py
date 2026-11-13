"""FEATURE_DEFAULTS：默认关的 key 必须显式 true 才能打开。"""
from __future__ import annotations

from feature_gate import FEATURE_DEFAULTS, FEATURE_DEFS, FeatureGate


def test_memory_layers_registered_but_default_off(db):
    gate = FeatureGate(db)
    assert "memory_layers" in {f["key"] for f in FEATURE_DEFS}
    assert FEATURE_DEFAULTS["memory_layers"] is False
    assert gate.is_enabled("group", "g1", "memory_layers") is False
    assert gate.effective_map("group", "g1")["memory_layers"] is False


def test_explicit_true_persists_and_false_removes_key(db):
    gate = FeatureGate(db)
    gate.set_enabled("group", "g1", "memory_layers", True)

    assert gate._raw_json("group", "g1") == {"memory_layers": True}
    assert gate.is_enabled("group", "g1", "memory_layers") is True

    gate.set_enabled("group", "g1", "memory_layers", False)
    assert "memory_layers" not in gate._raw_json("group", "g1")
    assert gate.is_enabled("group", "g1", "memory_layers") is False


def test_default_on_keys_keep_old_semantics(db):
    gate = FeatureGate(db)
    assert gate.is_enabled("group", "g1", "affection") is True

    gate.set_enabled("group", "g1", "affection", False)
    assert gate._raw_json("group", "g1") == {"affection": False}
    assert gate.is_enabled("group", "g1", "affection") is False

    gate.set_enabled("group", "g1", "affection", True)
    assert gate._raw_json("group", "g1") == {}
    assert gate.is_enabled("group", "g1", "affection") is True


def test_user_scope_isolated_from_group(db):
    gate = FeatureGate(db)
    gate.set_enabled("group", "g1", "memory_layers", True)

    assert gate.is_enabled("user", "u1", "memory_layers") is False
    assert gate.effective_map("user", "u1")["memory_layers"] is False


def test_unknown_key_is_enabled(db):
    assert FeatureGate(db).is_enabled("group", "g1", "not_a_real_key") is True
