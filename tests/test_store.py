"""store.py 测试"""

import json
from pathlib import Path

from src.core.store import Store


class TestStore:
    def test_new_store_has_no_seen(self, tmp_path):
        store = Store(tmp_path / "test.json")
        assert store.seen_count == 0
        assert not store.is_seen("abc")

    def test_mark_seen(self, tmp_path):
        store = Store(tmp_path / "test.json")
        store.mark_seen("id1", "Bug1")
        assert store.is_seen("id1")
        assert not store.is_seen("id2")
        assert store.seen_count == 1

    def test_filter_new_returns_only_unseen(self, tmp_path):
        store = Store(tmp_path / "test.json")
        store.mark_seen("old1")

        defects = [
            {"uuid": "old1", "name": "Old Bug"},
            {"uuid": "new1", "name": "New Bug 1"},
            {"uuid": "new2", "name": "New Bug 2"},
        ]
        new = store.filter_new(defects)
        assert len(new) == 2
        assert new[0]["uuid"] == "new1"
        assert new[1]["uuid"] == "new2"

    def test_filter_new_marks_as_seen(self, tmp_path):
        store = Store(tmp_path / "test.json")
        defects = [{"uuid": "a", "name": "Bug A"}, {"uuid": "b", "name": "Bug B"}]
        store.filter_new(defects)
        # 再次过滤应该返回空
        new = store.filter_new(defects)
        assert len(new) == 0

    def test_persistence(self, tmp_path):
        path = tmp_path / "test.json"
        store = Store(path)
        store.mark_seen("id1", "Bug1")

        # 重新加载
        store2 = Store(path)
        assert store2.is_seen("id1")
        assert store2.seen_count == 1

    def test_update_check_time(self, tmp_path):
        store = Store(tmp_path / "test.json")
        assert store.last_check is None
        store.update_check_time()
        assert store.last_check is not None

    def test_reset(self, tmp_path):
        store = Store(tmp_path / "test.json")
        store.mark_seen("id1")
        store.update_check_time()
        store.reset()
        assert store.seen_count == 0
        assert store.last_check is None
