from contextlib import closing
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN.parent)]

from team import DEFAULT_ROOM, TeamStore


class TeamHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.store = TeamStore(directory / "team.sqlite3")

    def test_opening_existing_data_adds_indexes_and_retains_legacy_history(self):
        names = {"room_by_round", "room_group_history", "casual_round_by_room"}
        with closing(self.store.connect()) as db, db:
            db.execute("DROP INDEX room_by_round")
            db.execute("DROP INDEX room_group_history")
            db.execute("DROP INDEX casual_round_by_room")
        self.store.add_line("legacy", "casual", "学伴", "旧版默认房间")
        self.store.add_line("parallel", "parallel", "学伴", "并行结果")
        self.store.add_line("group", "chat", "你", "群聊提问")
        self.store.add_line("default", "casual", "你", "新版默认房间", casual_key=DEFAULT_ROOM)

        reopened = TeamStore(self.store.path)

        with closing(reopened.connect()) as db:
            indexes = {row["name"] for row in db.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")}
        self.assertTrue(names <= indexes)
        self.assertEqual([row["body"] for row in reopened.lines(casual_key=DEFAULT_ROOM)],
                         ["旧版默认房间", "新版默认房间"])
        self.assertEqual([row["display_kind"] for row in reopened.lines(kinds=("chat", "task"))],
                         ["parallel", "chat"])
        self.assertEqual([row["body"] for row in reopened.lines(round_id="parallel")], ["并行结果"])

    def test_sparse_and_recent_rooms_return_only_the_requested_newest_messages(self):
        self.store.add_line("sparse", "casual", "你", "第一条", casual_key="小房间")
        self.store.add_line("sparse", "casual", "学伴", "第二条")
        self.store.add_line("active", "casual", "你", "旧消息", casual_key="活跃房间")
        for index in range(12):
            self.store.add_line("active", "casual", "学伴", f"活跃消息 {index}")

        self.assertEqual([row["body"] for row in self.store.lines(3, casual_key="小房间")], ["第一条", "第二条"])
        self.assertEqual([row["body"] for row in self.store.lines(3, casual_key="活跃房间")],
                         ["活跃消息 9", "活跃消息 10", "活跃消息 11"])
        self.assertEqual(self.store.lines(3, casual_key="空房间"), [])


if __name__ == "__main__":
    unittest.main()
