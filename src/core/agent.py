"""ONES Defect Agent - 增量监控 + 代码根因分析"""

from __future__ import annotations

import time
import threading
from datetime import datetime

from src.integrations.ones import OnesClient
from src.llm.analyzer import Analyzer
from src.integrations.codebase import Codebase
from src.core.store import Store
from src.integrations.wechat import WeChatBot


class DefectAgent:
    def __init__(
        self,
        ones: OnesClient | None = None,
        analyzer: Analyzer | None = None,
        bot: WeChatBot | None = None,
        store: Store | None = None,
        codebase: Codebase | None = None,
        codebase_path: str | None = None,
        repo_url: str | None = None,
        branch: str = "main",
    ):
        self.ones = ones or OnesClient()
        self.analyzer = analyzer or Analyzer()
        self.bot = bot or WeChatBot()
        self.store = store or Store()
        self.codebase = codebase or (
            Codebase(path=codebase_path, repo_url=repo_url, branch=branch)
            if codebase_path or repo_url else None
        )

    def check_new(self, mine: bool = True, **kwargs) -> list[dict]:
        all_defects = self.fetch(mine=mine, **kwargs)
        new = self.store.filter_new(all_defects)
        self.store.update_check_time()
        return new

    def fetch(self, mine: bool = True, **kwargs) -> list[dict]:
        if mine:
            return self.ones.fetch_my_defects(**kwargs)
        return self.ones.fetch_defects(**kwargs)

    def analyze(self, defects: list[dict]) -> list[dict]:
        return self.analyzer.batch_analyze(defects, codebase=self.codebase)

    def notify(self, results: list[dict]) -> dict:
        return self.bot.send_defect_report(results)

    def run_once(self, mine: bool = True, push: bool = True, **kwargs) -> list[dict]:
        new_defects = self.check_new(mine=mine, **kwargs)
        if not new_defects:
            if push:
                self.bot.send_text("✅ 无新增缺陷")
            return []

        results = self.analyze(new_defects)
        if push:
            self.notify(results)
        return results

    def run_schedule(self, interval: int = 1800, mine: bool = True, **kwargs):
        print(f"🤖 Agent 启动，每 {interval}s 检测一次新缺陷")
        if self.codebase and self.codebase.path:
            print(f"📂 代码仓库: {self.codebase.path}")
        while True:
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n⏰ [{ts}] 检测新缺陷...")
                results = self.run_once(mine=mine, **kwargs)
                if results:
                    print(f"🆕 发现 {len(results)} 个新缺陷，已分析并推送")
                else:
                    print("✅ 无新增缺陷")
            except Exception as e:
                print(f"❌ 执行失败: {e}")
            time.sleep(interval)

    def run_schedule_background(self, **kwargs) -> threading.Thread:
        t = threading.Thread(target=self.run_schedule, kwargs=kwargs, daemon=True)
        t.start()
        return t

    @staticmethod
    def quick_report(mine: bool = True, push: bool = True, **kwargs) -> list[dict]:
        return DefectAgent().run_once(mine=mine, push=push, **kwargs)
