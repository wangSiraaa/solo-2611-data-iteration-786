"""盲样比对服务测试：盲码冻结、开放期隐私、中位数资格、更正重评、持久化。"""

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from blend.blinding import (
    QUALIFIED,
    FAILED,
    UNRATABLE,
    BlindingService,
)
from blend.model import ValidationError

F = Fraction


class BlindingRoundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = BlindingService(":memory:")
        for oid in ("O-A", "O-B", "O-C", "O-D"):
            self.svc.register_organization(oid)
        self.labs = ["LAB1", "LAB2", "LAB3", "LAB4"]
        for lab, oid in zip(self.labs, ("O-A", "O-B", "O-C", "O-D")):
            self.svc.register_lab(lab, oid)
        self.svc.create_round("R1", "cX", F(1))
        self.codes = self.svc.issue_blind_codes("R1", self.labs)

    def tearDown(self) -> None:
        self.svc.close()

    def test_blind_codes_are_independent_and_receipts_scoped(self):
        # 每个实验室独立盲码，互不相同且与实验室标识无关
        self.assertEqual(len(set(self.codes.values())), 4)
        self.assertTrue(all(len(code) >= 8 for code in self.codes.values()))
        for lab in self.labs:
            self.svc.submit_reading(self.codes[lab], 3)
        # 盲码视图只回本实验室回执，看不到任何其他读数/映射
        own = self.svc.receipts(self.codes["LAB1"])
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0].value, 3)
        self.assertFalse(own[0].superseded)
        with self.assertRaises(ValidationError):
            self.svc.receipts("not-a-real-code")
        # 轮次清单只含状态信息，不含读数
        self.assertEqual(
            set(self.svc.list_rounds()[0]),
            {"state", "tolerance", "note", "round_id", "component"},
        )

    def test_reading_correction_supersedes_but_keeps_history(self):
        rid1 = self.svc.submit_reading(self.codes["LAB2"], 3)
        rid2 = self.svc.submit_reading(self.codes["LAB2"], 5)
        receipts = self.svc.receipts(self.codes["LAB2"])
        self.assertEqual([r.reading_id for r in receipts], [rid1, rid2])
        self.assertTrue(receipts[0].superseded)
        self.assertFalse(receipts[1].superseded)
        self.assertEqual(receipts[1].value, 5)

    def test_worked_example_median_qualification(self):
        # 题面：读数 1、3、5、9，容差 1，报 5 的实验室不合格
        for lab, v in zip(self.labs, (1, 3, 5, 9)):
            self.svc.submit_reading(self.codes[lab], v)
        version = self.svc.close_round("R1")
        view = self.svc.get_closure(version)
        by_lab = {q.lab_id: q for q in view.qualifications}
        # LAB3=5：参照 peers[1,3,9] 中位数 3，差 2 > 容差 1
        self.assertEqual(by_lab["LAB3"].status, FAILED)
        self.assertEqual(by_lab["LAB3"].reference, F(3))
        self.assertEqual(by_lab["LAB3"].abs_diff, F(2))
        self.assertEqual(by_lab["LAB3"].peer_count, 3)
        # 边缘离群同样不满足容差（规则对所有实验室一致）
        self.assertEqual(by_lab["LAB1"].status, FAILED)
        self.assertEqual(by_lab["LAB1"].reference, F(5))  # peers[3,5,9]
        # 冻结读数具名、带盲码与来源编号
        frozen = {f.lab_id: f for f in view.frozen}
        self.assertEqual(frozen["LAB3"].value, 5)
        self.assertEqual(frozen["LAB3"].blind_code, self.codes["LAB3"])
        self.assertEqual(frozen["LAB3"].reading_id,
                         by_lab["LAB3"].reading_id)

    def test_even_peer_count_uses_exact_average(self):
        # 5 家实验室，被评者之外恰 4 家，偶数取中间两数精确平均
        self.svc.register_organization("O-E")
        self.svc.register_lab("LAB5", "O-E")
        self.svc.create_round("RE", "cE", F(1, 4))
        codes = self.svc.issue_blind_codes("RE", self.labs + ["LAB5"])
        for lab, v in zip(self.labs + ["LAB5"], (0, 2, 4, 6, 100)):
            self.svc.submit_reading(codes[lab], v)
        version = self.svc.close_round("RE")
        q5 = self.svc.get_closure(version).qualification("LAB5")
        self.assertEqual(q5.reference, F(3))          # (2+4)/2 精确平均
        self.assertEqual(q5.abs_diff, F(97))
        self.assertEqual(q5.status, FAILED)

    def test_insufficient_peers_is_unratable(self):
        svc2 = BlindingService(":memory:")
        svc2.register_organization("O1")
        svc2.register_lab("A", "O1")
        svc2.register_lab("B", "O1")
        svc2.create_round("SMALL", "c", F(1))
        codes = svc2.issue_blind_codes("SMALL", ["A", "B"])
        svc2.submit_reading(codes["A"], 1)
        svc2.submit_reading(codes["B"], 1)
        version = svc2.close_round("SMALL")
        for q in svc2.get_closure(version).qualifications:
            self.assertEqual(q.status, UNRATABLE)
            self.assertEqual(q.peer_count, 1)
            self.assertIsNone(q.reference)
        svc2.register_organization("O2")
        svc2.register_lab("C", "O2")
        # 未提交读数的实验室也是不可评
        svc2.create_round("EMPTY", "c", F(1))
        c2 = svc2.issue_blind_codes("EMPTY", ["A", "B", "C"])
        svc2.submit_reading(c2["A"], 1)
        svc2.submit_reading(c2["B"], 1)
        v2 = svc2.close_round("EMPTY")
        quals = {q.lab_id: q.status for q in svc2.get_closure(v2).qualifications}
        self.assertEqual(quals["C"], UNRATABLE)
        svc2.close()

    def test_closed_round_rejects_submission_and_trial_not_qualification(self):
        for lab, v in zip(self.labs, (1, 3, 5, 9)):
            self.svc.submit_reading(self.codes[lab], v)
        v1 = self.svc.close_round("R1")
        with self.assertRaises(ValidationError):
            self.svc.submit_reading(self.codes["LAB1"], 2)
        # 重开期间：已关闭版本仍是当前可采信版本，试查不产生资格
        self.svc.reopen_round("R1")
        self.assertEqual(self.svc.current_closure_version("R1"), v1)
        self.svc.submit_reading(self.codes["LAB2"], 5)  # 正式更正 3->5
        v2 = self.svc.close_round("R1")
        statuses = {q.lab_id: q.status
                    for q in self.svc.get_closure(v2).qualifications}
        self.assertEqual(statuses["LAB2"], QUALIFIED)
        self.assertEqual(statuses["LAB3"], QUALIFIED)
        # 旧版本原样保留，新旧接续
        self.assertEqual(
            self.svc.get_closure(v1).qualification("LAB3").status, FAILED
        )
        self.assertEqual(self.svc.get_closure(v2).supersedes_version, v1)
        self.assertEqual(
            [c["version"] for c in self.svc.list_closures("R1")], [v1, v2]
        )

    def test_persistence_across_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "pt.db")
            svc = BlindingService(path)
            svc.register_organization("O1")
            svc.register_lab("A", "O1")
            svc.create_round("RR", "c", F(1, 2))
            code = svc.issue_blind_codes("RR", ["A"])["A"]
            svc.submit_reading(code, "3/7")  # 有理数字符串
            version = svc.close_round("RR")
            svc.close()

            reopened = BlindingService(path)
            self.assertEqual(reopened.receipts(code)[0].value, F(3, 7))
            # 盲码映射、机构归属完整保持
            self.assertEqual(reopened.organization_of("A"), "O1")
            self.assertEqual(reopened.current_closure_version("RR"), version)
            self.assertEqual(
                reopened.get_closure(version).frozen[0].value, F(3, 7)
            )
            reopened.close()


if __name__ == "__main__":
    unittest.main()
