"""独立原料质量目录（SQLite 持久化，精确有理数端点）。

目录职责：

1. 维护每个 lot 的稳定 lot_id、非负整数可用克数、包装步长/成本，以及若干
   成分的质量分数闭区间 ``[lo, hi]``（端点为精确有理数，``0<=lo<=hi<=1``）；
2. 支持一次整体修订（区间与可用量同时改），修订在单个事务内提交，
   失败则目录整体保持修订前状态，旧修订号永久可查；
3. 对任意给定用量组合独立计算每成分的质量和上下界与分数上下界，
   以 :class:`~blend.model.UsageProduct` 形式公开产品；
4. 不感知订单、不处理优先级，可被规划器与仓库独立调用。

区间语义（独立区间算术）：同批每克的同一未知成分分数在 ``[lo,hi]`` 内任取，
成分之间不声明相关性；组合质量和的可达区间为各 lot 贡献区间的 Minkowski 和，
即上下界分别线性求和。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .blinding import QUALIFIED, BlindingService
from .model import (
    ComponentConstraint,
    Lot,
    UsageProduct,
    ValidationError,
)

__all__ = ["QualityCatalog", "GOV_DUAL_LAB_BLIND_REPORT"]

# 受控成分当前唯一支持的治理方式：双实验室盲样资格 + 双报告精确交集
GOV_DUAL_LAB_BLIND_REPORT = "DUAL_LAB_BLIND_REPORT"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cat_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cat_revision (
    rev        INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cat_lot_rev (
    rev        INTEGER NOT NULL,
    lot_id     TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    available_g INTEGER NOT NULL,
    step_g     INTEGER NOT NULL,
    cost_per_g INTEGER NOT NULL,
    fixed_fee  INTEGER NOT NULL,
    PRIMARY KEY (rev, lot_id)
);
CREATE TABLE IF NOT EXISTS cat_interval_rev (
    rev       INTEGER NOT NULL,
    lot_id    TEXT NOT NULL,
    component TEXT NOT NULL,
    lo_num    INTEGER NOT NULL,
    lo_den    INTEGER NOT NULL,
    hi_num    INTEGER NOT NULL,
    hi_den    INTEGER NOT NULL,
    PRIMARY KEY (rev, lot_id, component)
);
-- 受控成分：启用后其区间只能经证据化发布修订，手工改写入口被封锁
CREATE TABLE IF NOT EXISTS cat_controlled_component (
    component  TEXT PRIMARY KEY,
    enabled_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
-- 外部实验室报告登记（闭区间，端点仍是 [0,1] 的原料质量分数语义）
CREATE TABLE IF NOT EXISTS cat_lab_report (
    report_id     TEXT PRIMARY KEY,
    lab_id        TEXT NOT NULL,
    sample_id     TEXT NOT NULL,
    lot_id        TEXT NOT NULL,
    component     TEXT NOT NULL,
    lo_num        INTEGER NOT NULL,
    lo_den        INTEGER NOT NULL,
    hi_num        INTEGER NOT NULL,
    hi_den        INTEGER NOT NULL,
    registered_at TEXT NOT NULL
);
-- 每次外部受控发布 = 一次普通目录修订 + 一行证据头
CREATE TABLE IF NOT EXISTS cat_publication (
    pub_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    rev        INTEGER NOT NULL UNIQUE,
    lot_id     TEXT NOT NULL,
    component  TEXT NOT NULL,
    governance TEXT NOT NULL,
    sample_id  TEXT NOT NULL,
    pt_version INTEGER NOT NULL,
    round_id   TEXT NOT NULL,
    lo_num     INTEGER NOT NULL,
    lo_den     INTEGER NOT NULL,
    hi_num     INTEGER NOT NULL,
    hi_den     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
-- 发布时冻结的报告快照（含当时的机构归属）
CREATE TABLE IF NOT EXISTS cat_publication_report (
    pub_id    INTEGER NOT NULL,
    report_id TEXT NOT NULL,
    lab_id    TEXT NOT NULL,
    org_id    TEXT NOT NULL,
    lo_num    INTEGER NOT NULL,
    lo_den    INTEGER NOT NULL,
    hi_num    INTEGER NOT NULL,
    hi_den    INTEGER NOT NULL,
    PRIMARY KEY (pub_id, report_id)
);
-- 发布时冻结的盲样资格快照（含当时机构、盲码与全部评定数值）
CREATE TABLE IF NOT EXISTS cat_publication_qual (
    pub_id     INTEGER NOT NULL,
    lab_id     TEXT NOT NULL,
    org_id     TEXT NOT NULL,
    pt_version INTEGER NOT NULL,
    round_id   TEXT NOT NULL,
    blind_code TEXT NOT NULL,
    status     TEXT NOT NULL,
    reading_id INTEGER,
    reading_num INTEGER,
    reading_den INTEGER,
    ref_num    INTEGER,
    ref_den    INTEGER,
    diff_num   INTEGER,
    diff_den   INTEGER,
    tol_num    INTEGER NOT NULL,
    tol_den    INTEGER NOT NULL,
    PRIMARY KEY (pub_id, lab_id)
);
"""


# ------------------------------------------------------------ 证据只读视图


@dataclass(frozen=True)
class LabReportView:
    """登记在册的一份外部实验室闭区间报告。"""

    report_id: str
    lab_id: str
    sample_id: str
    lot_id: str
    component: str
    lo: Fraction
    hi: Fraction
    registered_at: str


@dataclass(frozen=True)
class ReportSnapshot:
    """发布证据中冻结的报告快照（机构归属按发布当时）。"""

    report_id: str
    lab_id: str
    org_id: str
    lo: Fraction
    hi: Fraction


@dataclass(frozen=True)
class QualificationSnapshot:
    """发布证据中冻结的盲样资格快照（具名、带盲码与来源读数编号）。"""

    lab_id: str
    org_id: str
    pt_version: int
    round_id: str
    blind_code: str
    status: str
    reading_id: int | None
    reading: Fraction | None
    reference: Fraction | None
    abs_diff: Fraction | None
    tolerance: Fraction


@dataclass(frozen=True)
class PublicationEvidence:
    """一次外部受控发布的完整证据链，可脱离比对服务单独核对。"""

    pub_id: int
    rev: int
    lot_id: str
    component: str
    governance: str
    sample_id: str
    pt_version: int
    round_id: str
    lo: Fraction
    hi: Fraction
    created_at: str
    note: str
    reports: tuple[ReportSnapshot, ...]
    qualifications: tuple[QualificationSnapshot, ...]


class QualityCatalog:
    """SQLite 支持的原料质量目录。

    Parameters
    ----------
    path:
        数据库文件路径；``":memory:"`` 为内存目录。文件库的状态在重开
        进程后完整保持。
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        blinding: BlindingService | None = None,
    ) -> None:
        self.path = str(path)
        # 比对服务可在构造时绑定，也可在发布调用处临时传入；纯手工维护的
        # 历史目录不绑定任何比对服务也能正常读取与修订。
        self.blinding = blinding
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "QualityCatalog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 内部工具
    def _current_rev(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM cat_meta WHERE key = 'current_rev'"
        ).fetchone()
        return None if row is None else int(row[0])

    @property
    def revision(self) -> int | None:
        """当前修订号；尚无任何修订时为 ``None``。"""
        return self._current_rev()

    def revisions(self) -> list[tuple[int, str, str]]:
        """全部历史修订 ``(rev, created_at, note)``，按修订号升序。"""
        return list(
            self._conn.execute(
                "SELECT rev, created_at, note FROM cat_revision ORDER BY rev"
            )
        )

    # ==================================================== 受控成分与报告
    def enable_controlled_component(self, component: str, note: str = "") -> None:
        """把某成分标记为受控成分（幂等启用，不可回退）。

        启用后：``replace_lots`` 不能再改变该成分的任何区间（手工入口被
        封锁），该成分只能凭双实验室报告 + 盲样资格经
        :meth:`publish_external_component` 发布修订。
        """
        if not component:
            raise ValidationError("成分名必须是非空字符串")
        try:
            self._conn.execute(
                "INSERT INTO cat_controlled_component(component, enabled_at, note)"
                " VALUES (?,?,?)",
                (component, datetime.now(timezone.utc).isoformat(), note),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"受控成分 {component!r} 已启用") from exc

    def controlled_components(self) -> tuple[str, ...]:
        """已启用受控成分，按名称排序。"""
        rows = self._conn.execute(
            "SELECT component FROM cat_controlled_component ORDER BY component"
        ).fetchall()
        return tuple(r[0] for r in rows)

    def is_controlled(self, component: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM cat_controlled_component WHERE component=?",
            (component,),
        ).fetchone() is not None

    def register_lab_report(
        self,
        report_id: str,
        lab_id: str,
        sample_id: str,
        lot_id: str,
        component: str,
        lo,
        hi,
    ) -> None:
        """登记一份外部实验室闭区间报告（端点为 [0,1] 精确分数）。

        登记不产生任何目录变更；报告只有在通过发布证据链校验后才会影响
        目录区间。
        """
        if not report_id:
            raise ValidationError("报告 id 必须是非空字符串")
        lo_f, hi_f = Fraction(lo), Fraction(hi)
        # 复用 Lot 的端点校验口径（[0,1]、不倒挂）
        Lot("_report_", 1, {component: (lo_f, hi_f)})
        try:
            self._conn.execute(
                "INSERT INTO cat_lab_report(report_id, lab_id, sample_id, lot_id,"
                " component, lo_num, lo_den, hi_num, hi_den, registered_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (report_id, lab_id, sample_id, lot_id, component,
                 lo_f.numerator, lo_f.denominator, hi_f.numerator, hi_f.denominator,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"报告 {report_id!r} 已登记") from exc

    def get_report(self, report_id: str) -> LabReportView:
        row = self._conn.execute(
            "SELECT report_id, lab_id, sample_id, lot_id, component, lo_num, lo_den,"
            " hi_num, hi_den, registered_at FROM cat_lab_report WHERE report_id=?",
            (report_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"报告 {report_id!r} 未登记")
        return LabReportView(
            report_id=row[0], lab_id=row[1], sample_id=row[2], lot_id=row[3],
            component=row[4], lo=Fraction(row[5], row[6]),
            hi=Fraction(row[7], row[8]), registered_at=row[9],
        )

    # ------------------------------------------------------------ 修订
    def replace_lots(self, lots: Iterable[Lot], note: str = "") -> int:
        """整体修订：用新 lot 集合替换当前目录，返回新修订号。

        校验与写入在单事务内完成；任何 lot 不合法、lot_id 重复或成分
        词表不一致都会回滚，目录（含修订号）整体保持修订前状态。
        可用量与区间在同一次修订里一起生效，不存在中间态。

        受控成分关卡：受控成分一经启用，本手工入口既不能改写它的区间，
        也不能把它从词表中丢弃（否则即是绕开证据要求）；受控区间的唯一
        修订路径是 :meth:`publish_external_component`。
        """
        lots = list(lots)
        # ---- 事务外先做纯数据校验，事务内只做落库 -------------------
        ids: list[str] = []
        component_sets: set[frozenset[str]] = set()
        for lot in lots:
            if not isinstance(lot, Lot):
                raise ValidationError("replace_lots 仅接受 Lot 对象")
            ids.append(lot.lot_id)
            component_sets.add(frozenset(lot.intervals))
        if len(ids) != len(set(ids)):
            dup = sorted({x for x in ids if ids.count(x) > 1})
            raise ValidationError(f"lot_id 重复: {dup}")
        if len(component_sets) > 1:
            raise ValidationError(
                "目录内所有 lot 必须共享同一组成分词表（缺失成分请显式给 [0,0]）"
            )
        self._guard_controlled(lots)

        return self._insert_revision(lots, note)

    def _guard_controlled(self, new_lots: Sequence[Lot]) -> None:
        """手工修订不得触碰受控成分：区间须与当前修订逐端点一致、不得丢词表。"""
        controlled = self.controlled_components()
        if not controlled:
            return
        prev_rev = self._current_rev()
        if prev_rev is None:
            raise ValidationError(
                f"受控成分已启用 {sorted(controlled)}，手工修订入口不得写入"
                "其区间；请走证据化发布"
            )
        cur_vocab = set(self.components(prev_rev))
        new_vocab = set(new_lots[0].intervals)
        dropped = [c for c in controlled if c not in new_vocab]
        if dropped:
            raise ValidationError(
                f"手工修订不得从词表丢弃受控成分 {sorted(dropped)}（绕开证据要求）"
            )
        cur_lots = {lot.lot_id: lot for lot in self.get_lots(prev_rev)}
        for lot in new_lots:
            old = cur_lots.get(lot.lot_id)
            for comp in controlled:
                if comp not in cur_vocab:
                    raise ValidationError(
                        f"受控成分 {comp!r} 不在当前目录中，手工入口不得新增，"
                        "请走证据化发布"
                    )
                if old is None:
                    raise ValidationError(
                        f"新 lot {lot.lot_id!r} 携带受控成分 {comp!r} 区间："
                        "受控区间只能经证据化发布写入"
                    )
                if lot.intervals[comp] != old.intervals[comp]:
                    raise ValidationError(
                        f"受控成分 {comp!r}（lot {lot.lot_id}）只能凭双实验室报告"
                        "+ 盲样资格经 publish_external_component 修订，"
                        "replace_lots 手工入口已封锁"
                    )

    def _insert_revision(self, lots: Sequence[Lot], note: str) -> int:
        """共享修订落库：单事务写修订头、lot 与区间，返回新修订号。"""
        prev_rev = self._current_rev()
        new_rev = 1 if prev_rev is None else prev_rev + 1
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN")
            self._conn.execute(
                "INSERT INTO cat_revision(rev, created_at, note) VALUES (?,?,?)",
                (new_rev, now, note),
            )
            for seq, lot in enumerate(lots):
                self._conn.execute(
                    "INSERT INTO cat_lot_rev"
                    "(rev, lot_id, seq, available_g, step_g, cost_per_g, fixed_fee)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        new_rev,
                        lot.lot_id,
                        seq,
                        lot.available_g,
                        lot.step_g,
                        lot.cost_per_g,
                        lot.fixed_fee,
                    ),
                )
                for comp, (lo, hi) in lot.intervals.items():
                    self._conn.execute(
                        "INSERT INTO cat_interval_rev"
                        "(rev, lot_id, component, lo_num, lo_den, hi_num, hi_den)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (
                            new_rev,
                            lot.lot_id,
                            comp,
                            lo.numerator,
                            lo.denominator,
                            hi.numerator,
                            hi.denominator,
                        ),
                    )
            self._conn.execute(
                "INSERT INTO cat_meta(key, value) VALUES('current_rev', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_rev),),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return new_rev

    # ------------------------------------------------------------ 查询
    def get_lots(self, rev: int | None = None) -> tuple[Lot, ...]:
        """取某修订（默认当前修订）的全部 lot，按修订写入顺序返回。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT lot_id, available_g, step_g, cost_per_g, fixed_fee "
            "FROM cat_lot_rev WHERE rev=? ORDER BY seq",
            (rev,),
        ).fetchall()
        out: list[Lot] = []
        for lot_id, avail, step, cpg, fee in rows:
            iv_rows = self._conn.execute(
                "SELECT component, lo_num, lo_den, hi_num, hi_den "
                "FROM cat_interval_rev WHERE rev=? AND lot_id=? ORDER BY component",
                (rev, lot_id),
            ).fetchall()
            intervals = {
                comp: (Fraction(ln, ld), Fraction(hn, hd))
                for comp, ln, ld, hn, hd in iv_rows
            }
            out.append(Lot(lot_id, avail, intervals, step, cpg, fee))
        return tuple(out)

    def get_lot(self, lot_id: str, rev: int | None = None) -> Lot:
        for lot in self.get_lots(rev):
            if lot.lot_id == lot_id:
                return lot
        raise ValidationError(f"lot {lot_id!r} 在修订 {rev} 中不存在")

    def components(self, rev: int | None = None) -> tuple[str, ...]:
        """该修订的成分词表（所有 lot 共享），按名称排序。"""
        rev = self._require_rev(rev)
        rows = self._conn.execute(
            "SELECT DISTINCT component FROM cat_interval_rev WHERE rev=? "
            "ORDER BY component",
            (rev,),
        ).fetchall()
        return tuple(r[0] for r in rows)

    def _require_rev(self, rev: int | None) -> int:
        cur = self._current_rev()
        if cur is None:
            raise ValidationError("目录尚无任何修订")
        if rev is None:
            return cur
        hit = self._conn.execute(
            "SELECT 1 FROM cat_revision WHERE rev=?", (rev,)
        ).fetchone()
        if hit is None:
            raise ValidationError(f"修订号 {rev} 不存在")
        return rev

    # ------------------------------------------------------------ 质量产品
    def product(
        self,
        usage: Mapping[str, int],
        rev: int | None = None,
    ) -> UsageProduct:
        """对给定用量组合独立计算质量边界产品。

        对每个成分 c::

            质量和下界 = Σ grams_i * lo_i,c
            质量和上界 = Σ grams_i * hi_i,c
            分数界     = 质量界 / total_g

        未知 lot、负用量或零总量都按 :class:`ValidationError` 拒绝。
        """
        rev = self._require_rev(rev)
        lots = {lot.lot_id: lot for lot in self.get_lots(rev)}
        clean: dict[str, int] = {}
        for lot_id, grams in usage.items():
            if lot_id not in lots:
                raise ValidationError(f"用量中出现未知 lot: {lot_id!r}")
            if not isinstance(grams, int) or grams < 0:
                raise ValidationError(f"lot {lot_id} 用量必须是非负整数")
            if grams:
                clean[lot_id] = grams

        total = sum(clean.values())
        if total <= 0:
            raise ValidationError("用量合计必须为正整数")

        comps = self.components(rev)
        mass_bounds: dict[str, tuple[Fraction, Fraction]] = {}
        frac_bounds: dict[str, tuple[Fraction, Fraction]] = {}
        for comp in comps:
            m_lo = Fraction(0)
            m_hi = Fraction(0)
            for lot_id, grams in clean.items():
                lo, hi = lots[lot_id].intervals[comp]
                m_lo += grams * lo
                m_hi += grams * hi
            mass_bounds[comp] = (m_lo, m_hi)
            frac_bounds[comp] = (m_lo / total, m_hi / total)

        return UsageProduct(
            usage=dict(clean),
            total_g=total,
            mass_bounds=mass_bounds,
            fraction_bounds=frac_bounds,
            source=f"catalog@rev{rev}",
            components=frozenset(comps),
        )

    def robust_feasible(
        self,
        usage: Mapping[str, int],
        constraints: Sequence[ComponentConstraint],
        rev: int | None = None,
    ) -> bool:
        """便捷入口：用量组合对约束是否稳健合格。"""
        return self.product(usage, rev).is_robust_feasible(constraints)

    # ====================================== 外部受控成分证据化发布
    def publish_external_component(
        self,
        lot_id: str,
        component: str,
        governance: str,
        report_ids: Sequence[str],
        pt_version: int,
        blinding: BlindingService | None = None,
        note: str = "",
    ) -> PublicationEvidence:
        """凭双实验室报告 + 指定盲样关闭版本发布受控成分区间修订。

        全部校验与落库在单事务内完成，任一条件不满足则整次修订失败、
        修订号不动：

        1. ``governance`` 必须是受控双盲报告治理方式，且成分已启用受控；
        2. 恰好两份报告，且都指向**相同实物样本、相同 lot、相同成分**；
        3. 两位签发实验室在指定**当前关闭版本**中均为 QUALIFIED——开放期
           试报、旧版本结论都不采信；
        4. 两位实验室分属发布当时登记的**不同运营机构**；
        5. 两份闭区间有交集，采用其精确交集（Fraction 端点，无浮点）。

        发布时把报告、机构归属与盲样资格结论整体冻结成证据快照；后续
        机构归并或比对重评都不能改写已发布产品，新发布按新事实重判。
        """
        svc = blinding or self.blinding
        if svc is None:
            raise ValidationError(
                "受控成分发布需要绑定盲样比对服务（构造或调用时传入 blinding）"
            )
        if governance != GOV_DUAL_LAB_BLIND_REPORT:
            raise ValidationError(f"不支持的治理方式: {governance!r}")
        if not self.is_controlled(component):
            raise ValidationError(
                f"成分 {component!r} 尚未启用受控，不能走外部受控发布"
            )
        if len(report_ids) != 2 or len(set(report_ids)) != 2:
            raise ValidationError("受控发布必须恰好提供两份不同的实验室报告")

        # ---- 事务外收集并校验全部事实（失败即整体拒绝，不留半成品） ----
        reports = [self._require_report(rid) for rid in report_ids]
        for rep in reports:
            if (rep.sample_id, rep.lot_id, rep.component) != (
                reports[0].sample_id, lot_id, component
            ):
                raise ValidationError(
                    f"报告 {rep.report_id!r} 的样本/lot/成分 "
                    f"({rep.sample_id},{rep.lot_id},{rep.component}) 与发布请求 "
                    f"({reports[0].sample_id},{lot_id},{component}) 不一致"
                )

        closure = svc.get_closure(pt_version)  # 版本不存在直接抛错
        if closure.component != component:
            raise ValidationError(
                f"比对版本 {pt_version} 针对成分 {closure.component!r}，"
                f"与发布成分 {component!r} 不符"
            )
        current_version = svc.current_closure_version(closure.round_id)
        if current_version != pt_version:
            raise ValidationError(
                f"指定版本 {pt_version} 不是轮次 {closure.round_id!r} 的当前关闭"
                f"版本（当前为 {current_version}），不得拿旧结论发布"
            )

        lab_ids = [rep.lab_id for rep in reports]
        orgs: dict[str, str] = {}
        for lab_id in lab_ids:
            qual = closure.qualification(lab_id)
            if qual.status != QUALIFIED:
                raise ValidationError(
                    f"实验室 {lab_id!r} 在比对版本 {pt_version} 状态为 "
                    f"{qual.status}（须 QUALIFIED），发布拒绝"
                )
            orgs[lab_id] = svc.organization_of(lab_id)
        if orgs[lab_ids[0]] == orgs[lab_ids[1]]:
            raise ValidationError(
                f"两位签发实验室 {lab_ids} 同属机构 {orgs[lab_ids[0]]!r}，"
                "不满足运营机构独立性，发布拒绝"
            )

        lo = max(rep.lo for rep in reports)
        hi = min(rep.hi for rep in reports)
        if lo > hi:
            raise ValidationError(
                f"两份报告区间不相交：{[ (str(rep.lo), str(rep.hi)) for rep in reports ]}，"
                "整次目录修订失败"
            )

        prev_rev = self._current_rev()
        if prev_rev is None:
            raise ValidationError(
                "目录尚无任何修订：请先用 replace_lots 建立含该 lot/成分的基线"
            )
        prev_lots = list(self.get_lots(prev_rev))
        if not any(lot.lot_id == lot_id for lot in prev_lots):
            raise ValidationError(f"lot {lot_id!r} 不在当前目录中，无法发布受控区间")
        new_lots = tuple(
            lot if lot.lot_id != lot_id else self._with_interval(lot, component, lo, hi)
            for lot in prev_lots
        )

        # ---- 单事务：普通修订 + 证据头 + 报告/资格快照 ----------------
        now = datetime.now(timezone.utc).isoformat()
        try:
            self._conn.execute("BEGIN")
            new_rev = self._insert_revision_locked(new_lots, note, now)
            cur = self._conn.execute(
                "INSERT INTO cat_publication(rev, lot_id, component, governance,"
                " sample_id, pt_version, round_id, lo_num, lo_den, hi_num, hi_den,"
                " created_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_rev, lot_id, component, governance, reports[0].sample_id,
                 pt_version, closure.round_id, lo.numerator, lo.denominator,
                 hi.numerator, hi.denominator, now, note),
            )
            pub_id = int(cur.lastrowid)
            for rep in reports:
                self._conn.execute(
                    "INSERT INTO cat_publication_report(pub_id, report_id, lab_id,"
                    " org_id, lo_num, lo_den, hi_num, hi_den)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (pub_id, rep.report_id, rep.lab_id, orgs[rep.lab_id],
                     rep.lo.numerator, rep.lo.denominator,
                     rep.hi.numerator, rep.hi.denominator),
                )
            for lab_id in lab_ids:
                qual = closure.qualification(lab_id)
                frozen = next(f for f in closure.frozen if f.lab_id == lab_id)
                self._conn.execute(
                    "INSERT INTO cat_publication_qual(pub_id, lab_id, org_id,"
                    " pt_version, round_id, blind_code, status, reading_id,"
                    " reading_num, reading_den, ref_num, ref_den, diff_num,"
                    " diff_den, tol_num, tol_den)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pub_id, lab_id, orgs[lab_id], pt_version, closure.round_id,
                     frozen.blind_code, qual.status, qual.reading_id,
                     qual.reading.numerator if qual.reading is not None else None,
                     qual.reading.denominator if qual.reading is not None else None,
                     qual.reference.numerator if qual.reference is not None else None,
                     qual.reference.denominator if qual.reference is not None else None,
                     qual.abs_diff.numerator if qual.abs_diff is not None else None,
                     qual.abs_diff.denominator if qual.abs_diff is not None else None,
                     qual.tolerance.numerator, qual.tolerance.denominator),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return self.get_publication_by_rev(new_rev)

    def _require_report(self, report_id: str) -> LabReportView:
        return self.get_report(report_id)

    @staticmethod
    def _with_interval(
        lot: Lot, component: str, lo: Fraction, hi: Fraction
    ) -> Lot:
        intervals = dict(lot.intervals)
        intervals[component] = (lo, hi)
        return Lot(
            lot.lot_id, lot.available_g, intervals, lot.step_g,
            lot.cost_per_g, lot.fixed_fee,
        )

    def _insert_revision_locked(
        self, lots: Sequence[Lot], note: str, now: str
    ) -> int:
        """与 :meth:`_insert_revision` 同构，但复用外层事务与时间戳。"""
        prev_rev = self._current_rev()
        new_rev = 1 if prev_rev is None else prev_rev + 1
        self._conn.execute(
            "INSERT INTO cat_revision(rev, created_at, note) VALUES (?,?,?)",
            (new_rev, now, note),
        )
        for seq, lot in enumerate(lots):
            self._conn.execute(
                "INSERT INTO cat_lot_rev"
                "(rev, lot_id, seq, available_g, step_g, cost_per_g, fixed_fee)"
                " VALUES (?,?,?,?,?,?,?)",
                (new_rev, lot.lot_id, seq, lot.available_g, lot.step_g,
                 lot.cost_per_g, lot.fixed_fee),
            )
            for comp, (lo, hi) in lot.intervals.items():
                self._conn.execute(
                    "INSERT INTO cat_interval_rev"
                    "(rev, lot_id, component, lo_num, lo_den, hi_num, hi_den)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (new_rev, lot.lot_id, comp, lo.numerator, lo.denominator,
                     hi.numerator, hi.denominator),
                )
        self._conn.execute(
            "INSERT INTO cat_meta(key, value) VALUES('current_rev', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(new_rev),),
        )
        return new_rev

    # ------------------------------------------------------ 发布证据查询
    def list_publications(self) -> list[int]:
        """全部发布 id，按 id 升序。"""
        return [r[0] for r in self._conn.execute(
            "SELECT pub_id FROM cat_publication ORDER BY pub_id"
        ).fetchall()]

    def get_publication(self, pub_id: int) -> PublicationEvidence:
        return self._load_publication("pub_id", pub_id)

    def get_publication_by_rev(self, rev: int) -> PublicationEvidence:
        return self._load_publication("rev", rev)

    def _load_publication(self, key: str, value: int) -> PublicationEvidence:
        head = self._conn.execute(
            "SELECT pub_id, rev, lot_id, component, governance, sample_id,"
            " pt_version, round_id, lo_num, lo_den, hi_num, hi_den, created_at, note"
            f" FROM cat_publication WHERE {key}=?",
            (value,),
        ).fetchone()
        if head is None:
            raise ValidationError(f"发布证据（{key}={value}）不存在")
        rep_rows = self._conn.execute(
            "SELECT report_id, lab_id, org_id, lo_num, lo_den, hi_num, hi_den"
            " FROM cat_publication_report WHERE pub_id=? ORDER BY report_id",
            (head[0],),
        ).fetchall()
        reports = tuple(
            ReportSnapshot(
                report_id=r[0], lab_id=r[1], org_id=r[2],
                lo=Fraction(r[3], r[4]), hi=Fraction(r[5], r[6]),
            )
            for r in rep_rows
        )
        qual_rows = self._conn.execute(
            "SELECT lab_id, org_id, pt_version, round_id, blind_code, status,"
            " reading_id, reading_num, reading_den, ref_num, ref_den, diff_num,"
            " diff_den, tol_num, tol_den"
            " FROM cat_publication_qual WHERE pub_id=? ORDER BY lab_id",
            (head[0],),
        ).fetchall()
        quals = tuple(
            QualificationSnapshot(
                lab_id=r[0], org_id=r[1], pt_version=r[2], round_id=r[3],
                blind_code=r[4], status=r[5], reading_id=r[6],
                reading=Fraction(r[7], r[8]) if r[7] is not None else None,
                reference=Fraction(r[9], r[10]) if r[9] is not None else None,
                abs_diff=Fraction(r[11], r[12]) if r[11] is not None else None,
                tolerance=Fraction(r[13], r[14]),
            )
            for r in qual_rows
        )
        return PublicationEvidence(
            pub_id=head[0], rev=head[1], lot_id=head[2], component=head[3],
            governance=head[4], sample_id=head[5], pt_version=head[6],
            round_id=head[7], lo=Fraction(head[8], head[9]),
            hi=Fraction(head[10], head[11]), created_at=head[12], note=head[13],
            reports=reports, qualifications=quals,
        )

    def publication_rev_for(self, lot_id: str, component: str) -> int | None:
        """该 lot/成分最近一次外部受控发布对应的修订号（无则 None）。"""
        row = self._conn.execute(
            "SELECT rev FROM cat_publication WHERE lot_id=? AND component=?"
            " ORDER BY pub_id DESC LIMIT 1",
            (lot_id, component),
        ).fetchone()
        return None if row is None else int(row[0])
