from __future__ import annotations

import unittest
from pathlib import Path

from reversion_ops.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class ReversionAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        # 重复通知折叠为同一回转案例
        self.assertTrue(result["duplicate_notice_collapsed"])
        # 争议期只暂停受影响合作，共同开发继续
        self.assertTrue(result["co_development_kept_during_dispute"])
        self.assertTrue(result["sublicense_suspended_during_dispute"])
        self.assertEqual(result["unaffected_co_disposal"], "rejected")
        # 关闭前置未满足先阻断，满足后关闭；部分终止交易保持 active
        self.assertTrue(result["first_close_attempt"].startswith("blocked:"))
        self.assertEqual(result["closed_state"], "closed")
        self.assertEqual(result["deal_state_after_partial_close"], "active")
        # 迟到材料不重开
        self.assertFalse(result["late_material_reopened"])
        # 恢复自研/重新授权均经前置门控
        self.assertEqual(result["resume_first_attempt"], "blocked")
        # 只有中国大陆权利回转，境外仍在授权
        self.assertEqual(result["current_disposable_regions"], ["中国大陆"])
        self.assertEqual(result["licensed_territories_remaining"], ["t-row"])
        # 回转范围引用了原协议(0)与修订(1)
        self.assertEqual(result["agreement_revisions_cited"], [0, 1])
        # 无遗留未完成交接，时间线完整
        self.assertEqual(result["incomplete_handover"], [])
        self.assertTrue(result["timeline_valid"])
        self.assertGreater(result["timeline_events"], 0)
        self.assertEqual(result["schema"]["missing_tables"], [])


if __name__ == "__main__":
    unittest.main()
