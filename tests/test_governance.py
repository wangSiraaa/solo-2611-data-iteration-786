"""受控成分治理测试：证据化发布、同机构拒绝、防绕开关卡、证据快照与持久化。"""

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from blend.blinding import QUALIFIED, BlindingService
from blend.catalog import GOV_DUAL_LAB_BLIND_REPORT, QualityCatalog
from blend.model import ValidationError
from tests.factories import make_lot

F = Fraction


class GovernanceFixture:
    """四实验室（四个独立机构）+ 一轮 cX 比对 + 两 lot 基线目录。"""

    def __init__(self) -> None:
        self.svc = BlindingService(":memory:")
        for oid in ("O-A", "O-B", "O-C", "O-D"):
            self.svc.register_organization(oid)
        self.labs = ["LAB1", "LAB2", "LAB3", "LAB4"]
        for lab, oid in zip(self.labs, ("O-A", "O-B", "O-C", "O-D")):
            self.svc.register_lab(lab, oid)
        self.svc.create_round("R1", "cX", F(1))
        self.codes = self.svc.issue_blind_codes("R1", self.labs)
        for lab, v in zip(self.labs, (1, 3, 5, 9)):
            self.svc.submit_reading(self.codes[lab], v)
        self.v1 = self.svc.close_round("R1")
        # 正式更正 LAB2 3->5 后重评：LAB2、LAB3 双合格
        self.svc.reopen_round("R1")
        self.svc.submit_reading(self.codes["LAB2"], 5)
        self.v2 = self.svc.close_round("R1")

        self.cat = QualityCatalog(":memory:", self.svc)
        self.base_lots = [
            make_lot("L1", 100, {"cX": ("0", "1"), "cO": ("1/10", "1/5")}),
            make_lot("L2", 80, {"cX": ("0", "1"), "cO": ("1/4", "1/3")}),
        ]
        self.cat.replace_lots(self.base_lots, note="基线")
        self.cat.enable_controlled_component("cX")


class ControlledPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = GovernanceFixture()
        self.cat = self.fx.cat
        self.svc = self.fx.svc

    def tearDown(self) -> None:
        self.cat.close()
        self.svc.close()

    def _reports(self, sample="SMP-1"):
        # 题面区间：[1/5,2/5] 与 [3/10,1/2]，精确交集 [3/10,2/5]
        self.cat.register_lab_report(
            "RP-A", "LAB2", sample, "L1", "cX", F(1, 5), F(2, 5))
        self.cat.register_lab_report(
            "RP-B", "LAB3", sample, "L1", "cX", F(3, 10), F(1, 2))
        return ["RP-A", "RP-B"]

    def test_replace_lots_cannot_bypass_controlled_component(self):
        # 改受控区间 -> 拒绝，修订号不动
        rev_before = self.cat.revision
        bad_interval = [
            make_lot("L1", 100, {"cX": ("1/2", "1/2"), "cO": ("1/10", "1/5")}),
            make_lot("L2", 80, {"cX": ("0", "1"), "cO": ("1/4", "1/3")}),
        ]
        with self.assertRaises(ValidationError):
            self.cat.replace_lots(bad_interval)
        # 从词表丢弃受控成分 -> 同样拒绝
        dropped = [
            make_lot("L1", 100, {"cO": ("1/10", "1/5")}),
            make_lot("L2", 80, {"cO": ("1/4", "1/3")}),
        ]
        with self.assertRaises(ValidationError):
            self.cat.replace_lots(dropped)
        self.assertEqual(self.cat.revision, rev_before)
        # 不触碰受控成分的手工维护（库存、非受控区间）继续可用
        ok = [
            make_lot("L1", 70, {"cX": ("0", "1"), "cO": ("1/10", "1/5")}),
            make_lot("L2", 80, {"cX": ("0", "1"), "cO": ("1/4", "1/3")}),
        ]
        new_rev = self.cat.replace_lots(ok, note="只改库存")
        self.assertEqual(new_rev, rev_before + 1)
        self.assertEqual(self.cat.get_lot("L1").available_g, 70)
        # 历史手工成分与旧版质量产品继续可读
        old = self.cat.get_lots(rev=1)
        self.assertEqual(old[0].available_g, 100)

    def test_publish_intersection_and_evidence_snapshot(self):
        ids = self._reports()
        pub = self.cat.publish_external_component(
            "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT, ids, self.fx.v2)
        self.assertEqual((pub.lo, pub.hi), (F(3, 10), F(2, 5)))
        self.assertEqual(
            self.cat.get_lot("L1").intervals["cX"], (F(3, 10), F(2, 5))
        )
        # 修订即普通目录修订，产品口径与既有 product() 完全一致
        product = self.cat.product({"L1": 10, "L2": 0})
        self.assertEqual(product.fraction_bounds["cX"], (F(3, 10), F(2, 5)))
        # 证据冻结：报告、机构、盲码、来源读数编号
        self.assertEqual(len(pub.reports), 2)
        self.assertEqual({r.org_id for r in pub.reports}, {"O-B", "O-C"})
        for q in pub.qualifications:
            self.assertEqual(q.status, QUALIFIED)
            self.assertEqual(q.pt_version, self.fx.v2)
            self.assertTrue(q.blind_code)
            self.assertIsNotNone(q.reading_id)
        # 可按 rev 反查证据
        again = self.cat.get_publication_by_rev(pub.rev)
        self.assertEqual(again.pub_id, pub.pub_id)

    def test_stale_or_unqualified_version_rejected(self):
        ids = self._reports()
        # v1 中 LAB3 不合格 -> 拒绝
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT, ids, self.fx.v1)
        # 不存在的版本
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT, ids, 999)
        # 修订号未推进
        self.assertEqual(self.cat.revision, 1)

    def test_same_organization_rejected(self):
        ids = self._reports()
        good = self.cat.publish_external_component(
            "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT, ids, self.fx.v2)
        # 机构归并：LAB3 并入 LAB2 所在机构
        self.svc.merge_organizations("O-B", "O-C")
        self.assertEqual(self.svc.organization_of("LAB3"), "O-B")
        self.cat.register_lab_report(
            "RP-C", "LAB2", "SMP-2", "L1", "cX", F(1, 5), F(2, 5))
        self.cat.register_lab_report(
            "RP-D", "LAB3", "SMP-2", "L1", "cX", F(3, 10), F(1, 2))
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT,
                ["RP-C", "RP-D"], self.fx.v2)
        # 已发布证据快照不被归并改写
        frozen = self.cat.get_publication(good.pub_id)
        self.assertEqual(
            {q.org_id for q in frozen.qualifications}, {"O-B", "O-C"}
        )

    def test_disjoint_intervals_and_mismatched_sample_fail_whole_revision(self):
        rev_before = self.cat.revision
        self.svc.register_organization("O-E")
        self.svc.register_lab("LAB5", "O-E")
        self.svc.register_organization("O-F")
        self.svc.register_lab("LAB6", "O-F")
        self.svc.create_round("R2", "cX", F(10))
        codes = self.svc.issue_blind_codes("R2", ["LAB1", "LAB4", "LAB5", "LAB6"])
        for lab, v in zip(["LAB1", "LAB4", "LAB5", "LAB6"], (5, 5, 5, 6)):
            self.svc.submit_reading(codes[lab], v)
        v = self.svc.close_round("R2")

        # 不相交区间
        self.cat.register_lab_report(
            "RP-E", "LAB1", "SMP-3", "L1", "cX", F(0), F(1, 10))
        self.cat.register_lab_report(
            "RP-F", "LAB6", "SMP-3", "L1", "cX", F(9, 10), F(1))
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT,
                ["RP-E", "RP-F"], v)
        self.assertEqual(self.cat.revision, rev_before)

        # 样本/lot/成分不一致
        self.cat.register_lab_report(
            "RP-G", "LAB1", "SMP-X", "L1", "cX", F(1, 5), F(2, 5))
        self.cat.register_lab_report(
            "RP-H", "LAB6", "SMP-3", "L2", "cX", F(1, 5), F(2, 5))
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT,
                ["RP-G", "RP-H"], v)
        self.assertEqual(self.cat.revision, rev_before)

    def test_component_mismatch_and_governance_guards(self):
        ids = self._reports()
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cX", "UNKNOWN_GOV", ids, self.fx.v2)
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cO", GOV_DUAL_LAB_BLIND_REPORT, ids, self.fx.v2)
        with self.assertRaises(ValidationError):
            self.cat.publish_external_component(
                "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT,
                [ids[0]], self.fx.v2)  # 必须恰好两份

    def test_published_products_remain_readable_after_new_facts(self):
        ids = self._reports()
        pub1 = self.cat.publish_external_component(
            "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT, ids, self.fx.v2)
        # 第二次发布（新样本、新事实）
        self.svc.register_organization("O-E")
        self.svc.register_lab("LAB5", "O-E")
        self.svc.register_organization("O-F")
        self.svc.register_lab("LAB6", "O-F")
        self.svc.create_round("R2", "cX", F(10))
        codes = self.svc.issue_blind_codes("R2", ["LAB1", "LAB4", "LAB5", "LAB6"])
        for lab, v in zip(["LAB1", "LAB4", "LAB5", "LAB6"], (5, 5, 5, 6)):
            self.svc.submit_reading(codes[lab], v)
        v = self.svc.close_round("R2")
        self.cat.register_lab_report(
            "RP-I", "LAB6", "SMP-4", "L1", "cX", F(1, 3), F(3, 5))
        self.cat.register_lab_report(
            "RP-J", "LAB5", "SMP-4", "L1", "cX", F(2, 5), F(3, 4))
        pub2 = self.cat.publish_external_component(
            "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT, ["RP-I", "RP-J"], v)
        self.assertEqual((pub2.lo, pub2.hi), (F(2, 5), F(3, 5)))
        # 旧修订上的产品与证据原样复读
        old_lot = self.cat.get_lot("L1", rev=pub1.rev)
        self.assertEqual(old_lot.intervals["cX"], (F(3, 10), F(2, 5)))
        old_product = self.cat.product({"L1": 10}, rev=pub1.rev)
        self.assertEqual(
            old_product.fraction_bounds["cX"], (F(3, 10), F(2, 5)))
        self.assertEqual(
            self.cat.publication_rev_for("L1", "cX"), pub2.rev)


class PersistenceTest(unittest.TestCase):
    def test_reopen_keeps_evidence_chain(self):
        with tempfile.TemporaryDirectory() as d:
            pt_path = str(Path(d) / "pt.db")
            cat_path = str(Path(d) / "cat.db")
            svc = BlindingService(pt_path)
            for oid in ("O-A", "O-B", "O-C", "O-D"):
                svc.register_organization(oid)
            for lab, oid in zip(("LAB1", "LAB2", "LAB3", "LAB4"),
                                ("O-A", "O-B", "O-C", "O-D")):
                svc.register_lab(lab, oid)
            svc.create_round("R1", "cX", F(1))
            codes = svc.issue_blind_codes(
                "R1", ["LAB1", "LAB2", "LAB3", "LAB4"])
            for lab, v in zip(("LAB1", "LAB2", "LAB3", "LAB4"),
                              (4, 5, 5, 6)):
                svc.submit_reading(codes[lab], v)
            version = svc.close_round("R1")

            cat = QualityCatalog(cat_path, svc)
            cat.replace_lots([
                make_lot("L1", 50, {"cX": ("0", "1")}),
            ], note="基线")
            cat.enable_controlled_component("cX")
            cat.register_lab_report(
                "R1", "LAB2", "SMP", "L1", "cX", F(1, 5), F(2, 5))
            cat.register_lab_report(
                "R2", "LAB3", "SMP", "L1", "cX", F(3, 10), F(1, 2))
            pub = cat.publish_external_component(
                "L1", "cX", GOV_DUAL_LAB_BLIND_REPORT,
                ["R1", "R2"], version)
            cat.close()
            svc.close()

            # 重开两个库：目录区间、发布证据、比对资格全部可单独核对
            svc2 = BlindingService(pt_path)
            cat2 = QualityCatalog(cat_path, svc2)
            self.assertEqual(
                cat2.get_lot("L1").intervals["cX"], (F(3, 10), F(2, 5)))
            evidence = cat2.get_publication_by_rev(pub.rev)
            self.assertEqual(len(evidence.qualifications), 2)
            self.assertTrue(
                svc2.is_qualified(version, evidence.qualifications[0].lab_id))
            self.assertEqual(cat2.controlled_components(), ("cX",))
            # 重开后防绕开关卡仍然生效
            with self.assertRaises(ValidationError):
                cat2.replace_lots([
                    make_lot("L1", 50, {"cX": ("0", "1/2")})])
            cat2.close()
            svc2.close()


if __name__ == "__main__":
    unittest.main()
