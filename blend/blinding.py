"""盲样比对服务（SQLite 持久化，精确有理数读数，逐人中位数资格评定）。

服务按轮次（round）与成分，向不同实验室发放同一合成试样的独立盲码：

* **开放期**：实验室只凭本实验室盲码提交有理数读数（无量纲能力试验值，
  不受 [0,1] 约束），可多次提交更正本实验室记录；盲码视图只返回本实验室
  回执，不泄漏其他读数或盲码—实验室映射。
* **关闭轮次**：冻结各实验室最后一条有效读数，逐人评定——排除被评者后，
  用其他至少三家实验室的中位数作参照（偶数家取中间两数的精确平均），
  与参照的绝对差不大于轮次容差即取得该成分资格；参照实验室不足三家或
  本实验室无有效读数，标 ``UNRATABLE``（不可评）。
* **正式更正重评**：关闭后的轮次可重新开放，更正后再次关闭生成新版本，
  旧版本（含旧发布所依赖的资格结论）永久保留；开放中的试报不构成资格。

关闭结果具名（实验室 + 盲码 + 来源读数编号 + 参照值 + 差值），可逐行
单独核对。全部状态落 SQLite，文件库重开后完整保持。
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from .model import ValidationError, as_frac

__all__ = [
    "BlindingService",
    "Receipt",
    "FrozenReading",
    "QualificationView",
    "ClosureView",
    "ROUND_OPEN",
    "ROUND_CLOSED",
    "QUALIFIED",
    "FAILED",
    "UNRATABLE",
]

ROUND_OPEN = "OPEN"
ROUND_CLOSED = "CLOSED"
QUALIFIED = "QUALIFIED"
FAILED = "FAILED"
UNRATABLE = "UNRATABLE"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pt_organization (
    org_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pt_lab (
    lab_id     TEXT PRIMARY KEY,
    org_id     TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (org_id) REFERENCES pt_organization(org_id)
);
CREATE TABLE IF NOT EXISTS pt_round (
    round_id   TEXT PRIMARY KEY,
    component  TEXT NOT NULL,
    tol_num    INTEGER NOT NULL,
    tol_den    INTEGER NOT NULL,
    state      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pt_blind_code (
    blind_code TEXT PRIMARY KEY,
    round_id   TEXT NOT NULL,
    lab_id     TEXT NOT NULL,
    issued_at  TEXT NOT NULL,
    UNIQUE (round_id, lab_id),
    FOREIGN KEY (round_id) REFERENCES pt_round(round_id),
    FOREIGN KEY (lab_id) REFERENCES pt_lab(lab_id)
);
CREATE TABLE IF NOT EXISTS pt_reading (
    reading_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id     TEXT NOT NULL,
    lab_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    val_num      INTEGER NOT NULL,
    val_den      INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    superseded   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (round_id, lab_id, seq)
);
CREATE TABLE IF NOT EXISTS pt_closure (
    version            INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id           TEXT NOT NULL,
    seq                INTEGER NOT NULL,
    supersedes_version INTEGER,
    closed_at          TEXT NOT NULL,
    note               TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pt_closure_reading (
    version    INTEGER NOT NULL,
    lab_id     TEXT NOT NULL,
    blind_code TEXT NOT NULL,
    reading_id INTEGER NOT NULL,
    val_num    INTEGER NOT NULL,
    val_den    INTEGER NOT NULL,
    PRIMARY KEY (version, lab_id)
);
CREATE TABLE IF NOT EXISTS pt_qualification (
    version       INTEGER NOT NULL,
    lab_id        TEXT NOT NULL,
    status        TEXT NOT NULL,
    reading_id    INTEGER,
    ref_num       INTEGER,
    ref_den       INTEGER,
    diff_num      INTEGER,
    diff_den      INTEGER,
    tol_num       INTEGER NOT NULL,
    tol_den       INTEGER NOT NULL,
    peer_count    INTEGER NOT NULL,
    PRIMARY KEY (version, lab_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _frac_pair(x: Fraction) -> tuple[int, int]:
    return x.numerator, x.denominator


def _median(values: list[Fraction]) -> Fraction:
    """中位数；偶数个取中间两数的精确算术平均（Fraction，无浮点）。"""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


# ---------------------------------------------------------------- 只读视图


@dataclass(frozen=True)
class Receipt:
    """开放期盲码视图中的一条本实验室读数回执。"""

    reading_id: int
    seq: int
    value: Fraction
    submitted_at: str
    superseded: bool


@dataclass(frozen=True)
class FrozenReading:
    """关闭版本中冻结的某实验室最后有效读数（具名、带来源编号与盲码）。"""

    lab_id: str
    blind_code: str
    reading_id: int
    value: Fraction


@dataclass(frozen=True)
class QualificationView:
    """单实验室在某关闭版本中的资格评定结论，可逐行单独核对。"""

    version: int
    lab_id: str
    status: str
    reading_id: int | None
    reading: Fraction | None
    reference: Fraction | None
    abs_diff: Fraction | None
    tolerance: Fraction
    peer_count: int


@dataclass(frozen=True)
class ClosureView:
    """一次关闭（一个评定版本）的完整具名结果。"""

    version: int
    round_id: str
    seq: int
    component: str
    tolerance: Fraction
    closed_at: str
    supersedes_version: int | None
    note: str
    frozen: tuple[FrozenReading, ...]
    qualifications: tuple[QualificationView, ...]

    def qualification(self, lab_id: str) -> QualificationView:
        for q in self.qualifications:
            if q.lab_id == lab_id:
                return q
        raise ValidationError(f"版本 {self.version} 中无实验室 {lab_id!r} 的评定记录")

    def is_qualified(self, lab_id: str) -> bool:
        return self.qualification(lab_id).status == QUALIFIED


# ---------------------------------------------------------------- 服务主体


class BlindingService:
    """SQLite 支持的盲样比对服务。

    Parameters
    ----------
    path:
        数据库文件路径；``":memory:"`` 为内存库。文件库重开后轮次、盲码、
        读数历史（含被更正行）、关闭版本与资格结论完整保持。
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BlindingService":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ======================================================== 机构与实验室
    def register_organization(
        self, org_id: str, name: str = "", note: str = ""
    ) -> None:
        """登记运营机构（机构是资格独立性的判定口径）。"""
        self._nonempty(org_id, "机构 id")
        try:
            self._conn.execute(
                "INSERT INTO pt_organization(org_id, name, created_at, note)"
                " VALUES (?,?,?,?)",
                (org_id, name or org_id, _now(), note),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"机构 {org_id!r} 已登记") from exc

    def register_lab(self, lab_id: str, org_id: str, name: str = "") -> None:
        """把具名实验室登记到某运营机构名下。"""
        self._nonempty(lab_id, "实验室 id")
        if self._conn.execute(
            "SELECT 1 FROM pt_organization WHERE org_id=?", (org_id,)
        ).fetchone() is None:
            raise ValidationError(f"机构 {org_id!r} 未登记")
        try:
            self._conn.execute(
                "INSERT INTO pt_lab(lab_id, org_id, name, created_at)"
                " VALUES (?,?,?,?)",
                (lab_id, org_id, name or lab_id, _now()),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"实验室 {lab_id!r} 已登记") from exc

    def organization_of(self, lab_id: str) -> str:
        """实验室当前所属运营机构（机构归并后返回新机构）。"""
        row = self._conn.execute(
            "SELECT org_id FROM pt_lab WHERE lab_id=?", (lab_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"实验室 {lab_id!r} 未登记")
        return row[0]

    def labs(self, org_id: str | None = None) -> list[tuple[str, str]]:
        """实验室清单 ``(lab_id, org_id)``；可按机构过滤。"""
        if org_id is None:
            rows = self._conn.execute(
                "SELECT lab_id, org_id FROM pt_lab ORDER BY lab_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT lab_id, org_id FROM pt_lab WHERE org_id=? ORDER BY lab_id",
                (org_id,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def merge_organizations(self, into_org_id: str, from_org_id: str) -> None:
        """机构归并：把 ``from_org_id`` 名下实验室全部划入 ``into_org_id``。

        只改变**当前**归属；历史关闭版本与目录发布证据中冻结的机构快照
        不受影响，新发布须按归并后的新事实重新判断独立性。
        """
        if into_org_id == from_org_id:
            raise ValidationError("源机构与目标机构相同")
        for oid in (into_org_id, from_org_id):
            if self._conn.execute(
                "SELECT 1 FROM pt_organization WHERE org_id=?", (oid,)
            ).fetchone() is None:
                raise ValidationError(f"机构 {oid!r} 未登记")
        self._conn.execute(
            "UPDATE pt_lab SET org_id=? WHERE org_id=?",
            (into_org_id, from_org_id),
        )
        self._conn.commit()

    # ================================================================ 轮次
    def create_round(
        self, round_id: str, component: str, tolerance, note: str = ""
    ) -> None:
        """创建一轮某成分的盲样比对，容差为非负精确有理数。"""
        self._nonempty(round_id, "轮次 id")
        self._nonempty(component, "成分名")
        tol = as_frac(tolerance)
        if tol < 0:
            raise ValidationError("轮次容差必须是非负有理数")
        try:
            self._conn.execute(
                "INSERT INTO pt_round(round_id, component, tol_num, tol_den,"
                " state, created_at, note) VALUES (?,?,?,?,?,?,?)",
                (round_id, component, tol.numerator, tol.denominator,
                 ROUND_OPEN, _now(), note),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValidationError(f"轮次 {round_id!r} 已存在") from exc

    def list_rounds(self) -> list[dict]:
        """轮次清单（只含状态信息，不泄漏开放期读数）。"""
        rows = self._conn.execute(
            "SELECT round_id, component, tol_num, tol_den, state, note"
            " FROM pt_round ORDER BY round_id"
        ).fetchall()
        return [
            {
                "round_id": r[0],
                "component": r[1],
                "tolerance": Fraction(r[2], r[3]),
                "state": r[4],
                "note": r[5],
            }
            for r in rows
        ]

    def _round_row(self, round_id: str) -> tuple:
        row = self._conn.execute(
            "SELECT round_id, component, tol_num, tol_den, state"
            " FROM pt_round WHERE round_id=?",
            (round_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"轮次 {round_id!r} 不存在")
        return row

    def _require_state(self, round_id: str, state: str) -> tuple:
        row = self._round_row(round_id)
        if row[4] != state:
            raise ValidationError(
                f"轮次 {round_id!r} 当前状态为 {row[4]}，要求 {state}"
            )
        return row

    # ================================================================ 盲码
    def issue_blind_codes(
        self, round_id: str, lab_ids: list[str]
    ) -> dict[str, str]:
        """向各实验室发放同一合成试样的独立盲码，返回 ``lab_id -> 盲码``。

        每个轮次每个实验室只能领一次码；盲码全局唯一，发放映射在开放期
        不进入任何只读视图。
        """
        self._require_state(round_id, ROUND_OPEN)
        if not lab_ids:
            raise ValidationError("至少指定一个实验室")
        if len(lab_ids) != len(set(lab_ids)):
            raise ValidationError("实验室列表重复")
        out: dict[str, str] = {}
        try:
            self._conn.execute("BEGIN")
            for lab_id in lab_ids:
                if self._conn.execute(
                    "SELECT 1 FROM pt_lab WHERE lab_id=?", (lab_id,)
                ).fetchone() is None:
                    raise ValidationError(f"实验室 {lab_id!r} 未登记")
                if self._conn.execute(
                    "SELECT 1 FROM pt_blind_code WHERE round_id=? AND lab_id=?",
                    (round_id, lab_id),
                ).fetchone() is not None:
                    raise ValidationError(
                        f"实验室 {lab_id!r} 在轮次 {round_id!r} 已领盲码"
                    )
                # 冲突重试（实际碰撞概率可忽略，循环仅为约束兜底）
                while True:
                    code = secrets.token_hex(8)
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO pt_blind_code"
                        "(blind_code, round_id, lab_id, issued_at)"
                        " VALUES (?,?,?,?)",
                        (code, round_id, lab_id, _now()),
                    )
                    if cur.rowcount:
                        break
                out[lab_id] = code
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return out

    def _resolve_code(self, blind_code: str) -> tuple[str, str, str]:
        row = self._conn.execute(
            "SELECT round_id, lab_id FROM pt_blind_code WHERE blind_code=?",
            (blind_code,),
        ).fetchone()
        if row is None:
            raise ValidationError("盲码无效")
        return blind_code, row[0], row[1]

    # ================================================================ 读数
    def submit_reading(self, blind_code: str, value) -> int:
        """开放期凭盲码提交有理读数；重复提交即更正，返回新回执编号。

        旧读数不删除，标记为已取代（superseded）；轮次关闭后拒绝提交，
        重新开放期间提交的仍是试报，不改变任何已关闭版本的资格。
        """
        _, round_id, lab_id = self._resolve_code(blind_code)
        self._require_state(round_id, ROUND_OPEN)
        val = as_frac(value)  # 能力试验值：任意有理数，不做 [0,1] 限制
        try:
            self._conn.execute("BEGIN")
            seq_row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM pt_reading"
                " WHERE round_id=? AND lab_id=?",
                (round_id, lab_id),
            ).fetchone()
            seq = seq_row[0] + 1
            self._conn.execute(
                "UPDATE pt_reading SET superseded=1 WHERE round_id=? AND lab_id=?",
                (round_id, lab_id),
            )
            cur = self._conn.execute(
                "INSERT INTO pt_reading(round_id, lab_id, seq, val_num, val_den,"
                " submitted_at) VALUES (?,?,?,?,?,?)",
                (round_id, lab_id, seq, val.numerator, val.denominator, _now()),
            )
            reading_id = int(cur.lastrowid)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return reading_id

    def receipts(self, blind_code: str) -> tuple[Receipt, ...]:
        """盲码视图：只返回本实验室自己的全部回执（含被更正行）。"""
        _, round_id, lab_id = self._resolve_code(blind_code)
        rows = self._conn.execute(
            "SELECT reading_id, seq, val_num, val_den, submitted_at, superseded"
            " FROM pt_reading WHERE round_id=? AND lab_id=? ORDER BY seq",
            (round_id, lab_id),
        ).fetchall()
        return tuple(
            Receipt(
                reading_id=r[0],
                seq=r[1],
                value=Fraction(r[2], r[3]),
                submitted_at=r[4],
                superseded=bool(r[5]),
            )
            for r in rows
        )

    # ======================================================== 关闭与重评
    def close_round(self, round_id: str, note: str = "") -> int:
        """关闭轮次并评定，返回新关闭版本号（全局版本编号）。

        冻结时取每个实验室最后一条未取代读数；旧版本原样保留，新版本
        通过 ``supersedes_version`` 接续。
        """
        _, component, tol_num, tol_den, state = self._require_state(
            round_id, ROUND_OPEN
        )
        tolerance = Fraction(tol_num, tol_den)
        try:
            self._conn.execute("BEGIN")
            prev = self._conn.execute(
                "SELECT MAX(version) FROM pt_closure WHERE round_id=?",
                (round_id,),
            ).fetchone()[0]
            seq_row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM pt_closure WHERE round_id=?",
                (round_id,),
            ).fetchone()
            seq = seq_row[0] + 1
            now = _now()
            cur = self._conn.execute(
                "INSERT INTO pt_closure(round_id, seq, supersedes_version,"
                " closed_at, note) VALUES (?,?,?,?,?)",
                (round_id, seq, prev, now, note),
            )
            version = int(cur.lastrowid)
            self._conn.execute(
                "UPDATE pt_round SET state=?, closed_at=? WHERE round_id=?",
                (ROUND_CLOSED, now, round_id),
            )

            # ---- 冻结各实验室最后有效读数 ------------------------------
            issued = self._conn.execute(
                "SELECT lab_id, blind_code FROM pt_blind_code"
                " WHERE round_id=? ORDER BY lab_id",
                (round_id,),
            ).fetchall()
            frozen: dict[str, tuple[str, int, Fraction]] = {}
            for lab_id, blind_code in issued:
                last = self._conn.execute(
                    "SELECT reading_id, val_num, val_den FROM pt_reading"
                    " WHERE round_id=? AND lab_id=? AND superseded=0"
                    " ORDER BY seq DESC LIMIT 1",
                    (round_id, lab_id),
                ).fetchone()
                if last is None:
                    continue
                reading_id, vn, vd = last
                value = Fraction(vn, vd)
                frozen[lab_id] = (blind_code, reading_id, value)
                self._conn.execute(
                    "INSERT INTO pt_closure_reading"
                    "(version, lab_id, blind_code, reading_id, val_num, val_den)"
                    " VALUES (?,?,?,?,?,?)",
                    (version, lab_id, blind_code, reading_id, vn, vd),
                )

            # ---- 逐人评定：排除被评者，参照至少三家 --------------------
            for lab_id, blind_code in issued:
                if lab_id not in frozen:
                    self._insert_qual(version, lab_id, None, None, None,
                                      None, tolerance, 0)
                    continue
                _, reading_id, reading = frozen[lab_id]
                peer_values = [
                    v for other, (_, _, v) in frozen.items() if other != lab_id
                ]
                peer_count = len(peer_values)
                if peer_count < 3:
                    self._insert_qual(version, lab_id, reading_id, reading,
                                      None, None, tolerance, peer_count)
                    continue
                reference = _median(peer_values)
                diff = abs(reading - reference)
                status = QUALIFIED if diff <= tolerance else FAILED
                self._insert_qual(
                    version, lab_id, reading_id, reading, reference, diff,
                    tolerance, peer_count, status,
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return version

    def _insert_qual(
        self,
        version: int,
        lab_id: str,
        reading_id: int | None,
        reading: Fraction | None,
        reference: Fraction | None,
        diff: Fraction | None,
        tolerance: Fraction,
        peer_count: int,
        status: str = UNRATABLE,
    ) -> None:
        ref = _frac_pair(reference) if reference is not None else (None, None)
        dff = _frac_pair(diff) if diff is not None else (None, None)
        self._conn.execute(
            "INSERT INTO pt_qualification(version, lab_id, status, reading_id,"
            " ref_num, ref_den, diff_num, diff_den, tol_num, tol_den, peer_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (version, lab_id, status, reading_id, ref[0], ref[1],
             dff[0], dff[1], tolerance.numerator, tolerance.denominator, peer_count),
        )

    def reopen_round(self, round_id: str) -> None:
        """重新开放已关闭轮次，供正式更正；历史关闭版本一律保留。"""
        self._require_state(round_id, ROUND_CLOSED)
        self._conn.execute(
            "UPDATE pt_round SET state=? WHERE round_id=?",
            (ROUND_OPEN, round_id),
        )
        self._conn.commit()

    # ================================================================ 查询
    def list_closures(self, round_id: str | None = None) -> list[dict]:
        """关闭版本摘要（可按轮次过滤），按版本号升序。"""
        if round_id is None:
            rows = self._conn.execute(
                "SELECT c.version, c.round_id, r.component, c.seq,"
                " c.supersedes_version, c.closed_at, c.note, r.tol_num, r.tol_den"
                " FROM pt_closure c JOIN pt_round r ON r.round_id = c.round_id"
                " ORDER BY c.version"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT c.version, c.round_id, r.component, c.seq,"
                " c.supersedes_version, c.closed_at, c.note, r.tol_num, r.tol_den"
                " FROM pt_closure c JOIN pt_round r ON r.round_id = c.round_id"
                " WHERE c.round_id=? ORDER BY c.version",
                (round_id,),
            ).fetchall()
        return [
            {
                "version": r[0],
                "round_id": r[1],
                "component": r[2],
                "seq": r[3],
                "supersedes_version": r[4],
                "closed_at": r[5],
                "note": r[6],
                "tolerance": Fraction(r[7], r[8]),
            }
            for r in rows
        ]

    def current_closure_version(self, round_id: str) -> int | None:
        """该轮次最新关闭版本号；从未关闭返回 None（开放试报无资格）。"""
        row = self._conn.execute(
            "SELECT MAX(version) FROM pt_closure WHERE round_id=?",
            (round_id,),
        ).fetchone()
        return None if row[0] is None else int(row[0])

    def get_closure(self, version: int) -> ClosureView:
        """取某关闭版本的完整具名结果（冻结读数 + 逐人资格 + 来源编号）。"""
        head = self._conn.execute(
            "SELECT c.round_id, r.component, c.seq, c.supersedes_version,"
            " c.closed_at, c.note, r.tol_num, r.tol_den"
            " FROM pt_closure c JOIN pt_round r ON r.round_id = c.round_id"
            " WHERE c.version=?",
            (version,),
        ).fetchone()
        if head is None:
            raise ValidationError(f"关闭版本 {version} 不存在")
        frozen_rows = self._conn.execute(
            "SELECT lab_id, blind_code, reading_id, val_num, val_den"
            " FROM pt_closure_reading WHERE version=? ORDER BY lab_id",
            (version,),
        ).fetchall()
        frozen = tuple(
            FrozenReading(
                lab_id=r[0],
                blind_code=r[1],
                reading_id=r[2],
                value=Fraction(r[3], r[4]),
            )
            for r in frozen_rows
        )
        qual_rows = self._conn.execute(
            "SELECT lab_id, status, reading_id, ref_num, ref_den, diff_num,"
            " diff_den, tol_num, tol_den, peer_count"
            " FROM pt_qualification WHERE version=? ORDER BY lab_id",
            (version,),
        ).fetchall()
        quals = tuple(
            QualificationView(
                version=version,
                lab_id=r[0],
                status=r[1],
                reading_id=r[2],
                reading=(
                    self._reading_value(version, r[2])
                    if r[2] is not None else None
                ),
                reference=Fraction(r[3], r[4]) if r[3] is not None else None,
                abs_diff=Fraction(r[5], r[6]) if r[5] is not None else None,
                tolerance=Fraction(r[7], r[8]),
                peer_count=r[9],
            )
            for r in qual_rows
        )
        return ClosureView(
            version=version,
            round_id=head[0],
            seq=head[2],
            component=head[1],
            tolerance=Fraction(head[6], head[7]),
            closed_at=head[4],
            supersedes_version=head[3],
            note=head[5],
            frozen=frozen,
            qualifications=quals,
        )

    def _reading_value(self, version: int, reading_id: int) -> Fraction | None:
        row = self._conn.execute(
            "SELECT val_num, val_den FROM pt_closure_reading"
            " WHERE version=? AND reading_id=?",
            (version, reading_id),
        ).fetchone()
        return None if row is None else Fraction(row[0], row[1])

    def qualification(self, version: int, lab_id: str) -> QualificationView:
        return self.get_closure(version).qualification(lab_id)

    def is_qualified(self, version: int, lab_id: str) -> bool:
        """严格口径：仅当该实验室在指定**已关闭版本**中状态为 QUALIFIED。"""
        return self.get_closure(version).is_qualified(lab_id)

    # ------------------------------------------------------------ 内部工具
    @staticmethod
    def _nonempty(value: str, what: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValidationError(f"{what}必须是非空字符串")
