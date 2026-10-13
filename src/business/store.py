"""
业务系统数据层（SQLite）

为什么用 SQLite 而不是"直接编几个假字典"：
    工具选择实验里，如果 12 个干扰工具全是返回空壳的桩函数，
    模型会很快学到"只有前 3 个有内容"，准确率就不具备参考价值。
    所以 15 个工具全部是**同一份数据集上的真实查询**，只是切片不同。

为什么确定性种子（seed=20260928）：
    换台机器重新生成，订单号、库存数字必须完全一致，
    否则我今天测出来的"准确率 83%"明天复现不出来，数字就废了。

只读/可写分离：
    15 个工具里 14 个只读，只有 create_ticket 是写。
    写工具单独走 _write_conn()，并且在 MCP Server 层再套一层二次确认。
"""

from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

SEED = 20260928
DEFAULT_DB = os.getenv("MJ_BUSINESS_DB", "./.data/business.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
  id TEXT PRIMARY KEY, name TEXT, contact TEXT, lead_time_days INT, city TEXT);
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY, name TEXT, level TEXT, city TEXT, phone TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS employees (
  id TEXT PRIMARY KEY, name TEXT, dept TEXT, title TEXT, email TEXT);
CREATE TABLE IF NOT EXISTS warehouses (
  id TEXT PRIMARY KEY, name TEXT, city TEXT, capacity INT);
CREATE TABLE IF NOT EXISTS skus (
  sku TEXT PRIMARY KEY, name TEXT, category TEXT, unit_price REAL, supplier_id TEXT);
CREATE TABLE IF NOT EXISTS inventory (
  sku TEXT, warehouse_id TEXT, qty INT, safety_stock INT,
  PRIMARY KEY (sku, warehouse_id));
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, customer_id TEXT, status TEXT, amount REAL,
  pay_method TEXT, warehouse_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, sku TEXT, qty INT, price REAL);
CREATE TABLE IF NOT EXISTS shipments (
  id TEXT PRIMARY KEY, order_id TEXT, carrier TEXT, tracking_no TEXT,
  status TEXT, shipped_at TEXT);
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY, order_id TEXT, amount REAL, tax_no TEXT, issued_at TEXT);
CREATE TABLE IF NOT EXISTS refunds (
  id TEXT PRIMARY KEY, order_id TEXT, amount REAL, reason TEXT,
  status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS coupons (
  code TEXT PRIMARY KEY, type TEXT, value REAL, valid_until TEXT, used_by TEXT);
CREATE TABLE IF NOT EXISTS returns (
  id TEXT PRIMARY KEY, order_id TEXT, sku TEXT, qty INT, reason TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT, customer_id TEXT,
  rating INT, text TEXT);
CREATE TABLE IF NOT EXISTS contracts (
  id TEXT PRIMARY KEY, supplier_id TEXT, start_date TEXT, end_date TEXT, amount REAL);
CREATE TABLE IF NOT EXISTS price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT, changed_at TEXT,
  old_price REAL, new_price REAL);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, subject TEXT, body TEXT, priority TEXT,
  status TEXT, created_at TEXT);
"""

_FAMILY = ["张", "王", "李", "赵", "陈", "刘", "杨", "黄", "周", "吴"]
_GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "洋", "勇", "艳", "杰", "强", "军"]
_CITY = ["苏州", "上海", "杭州", "南京", "合肥", "无锡", "宁波", "成都"]
_CAT = ["连接器", "继电器", "传感器", "电源模块", "线缆组件", "端子台", "开关"]
_DEPT = ["销售部", "仓储部", "采购部", "客服部", "技术部", "财务部"]
_STATUS = ["已付款", "待发货", "已发货", "已完成", "已取消"]
_CARRIER = ["顺丰", "京东物流", "中通", "德邦"]
_REASON = ["客户改单", "质量问题", "发错货", "物流破损", "到货延迟"]


def db_path() -> str:
    return os.getenv("MJ_BUSINESS_DB", DEFAULT_DB)


def _connect(readonly: bool = False) -> sqlite3.Connection:
    p = db_path()
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    if readonly:
        # 只读连接：从驱动层挡住误写，比"靠自觉不调用写函数"可靠
        uri = f"file:{p}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(p)


def ensure_seeded(force: bool = False) -> str:
    """建库并写入确定性数据。已存在则跳过（除非 force）。"""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        if n and not force:
            return db_path()
        if force:
            for t in ("suppliers", "customers", "employees", "warehouses", "skus",
                      "inventory", "orders", "order_items", "shipments", "invoices",
                      "refunds", "coupons", "returns", "reviews", "contracts",
                      "price_history"):
                conn.execute(f"DELETE FROM {t}")
        _seed(conn)
        conn.commit()
        return db_path()
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    rnd = random.Random(SEED)
    base = datetime(2026, 1, 1)

    suppliers = [(f"S{100+i}", f"{rnd.choice(_CITY)}{rnd.choice(_CAT)}供应商",
                  f"138{rnd.randint(10000000, 99999999)}",
                  rnd.randint(3, 30), rnd.choice(_CITY)) for i in range(12)]
    conn.executemany("INSERT INTO suppliers VALUES (?,?,?,?,?)", suppliers)

    customers = [(f"C{1000+i}",
                  f"{rnd.choice(_FAMILY)}{rnd.choice(_GIVEN)}",
                  rnd.choice(["普通", "银牌", "金牌", "钻石"]),
                  rnd.choice(_CITY), f"139{rnd.randint(10000000, 99999999)}",
                  (base + timedelta(days=rnd.randint(0, 400))).strftime("%Y-%m-%d"))
                 for i in range(60)]
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", customers)

    employees = [(f"E{200+i}", f"{rnd.choice(_FAMILY)}{rnd.choice(_GIVEN)}",
                  rnd.choice(_DEPT), rnd.choice(["专员", "主管", "经理"]),
                  f"e{200+i}@mingjian.example") for i in range(40)]
    conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?)", employees)

    warehouses = [(f"W{10+i}", f"{c}仓", c, rnd.randint(5000, 20000))
                  for i, c in enumerate(["苏州", "上海", "杭州", "南京", "合肥"])]
    conn.executemany("INSERT INTO warehouses VALUES (?,?,?,?)", warehouses)

    skus = [(f"SKU-{10000+i*7}", f"{rnd.choice(_CAT)}{rnd.choice(['A', 'B', 'C'])}{i}",
             rnd.choice(_CAT), round(rnd.uniform(3.5, 480.0), 2),
             suppliers[rnd.randrange(12)][0]) for i in range(60)]
    conn.executemany("INSERT INTO skus VALUES (?,?,?,?,?)", skus)

    inventory = []
    for sku, *_ in skus:
        for w in warehouses:
            inventory.append((sku, w[0], rnd.randint(0, 900), rnd.choice([50, 80, 100])))
    conn.executemany("INSERT INTO inventory VALUES (?,?,?,?)", inventory)

    orders, items = [], []
    for i in range(240):
        oid = f"SO-{900000+i}"
        cid = customers[rnd.randrange(60)][0]
        wh = warehouses[rnd.randrange(5)][0]
        created = base + timedelta(days=rnd.randint(0, 260), hours=rnd.randint(0, 23))
        amount = 0.0
        for _ in range(rnd.randint(1, 4)):
            s = skus[rnd.randrange(60)]
            qty = rnd.randint(1, 50)
            price = s[3]
            amount += qty * price
            items.append((oid, s[0], qty, price))
        orders.append((oid, cid, rnd.choice(_STATUS), round(amount, 2),
                       rnd.choice(["电汇", "承兑", "在线支付"]), wh,
                       created.strftime("%Y-%m-%d %H:%M")))
    conn.executemany("INSERT INTO orders (id,customer_id,status,amount,pay_method,"
                     "warehouse_id,created_at) VALUES (?,?,?,?,?,?,?)", orders)
    conn.executemany("INSERT INTO order_items (order_id,sku,qty,price) VALUES (?,?,?,?)",
                     items)

    shipped = [o for o in orders if o[2] in ("已发货", "已完成")]
    shipments = [(f"SH-{7000+i}", o[0], rnd.choice(_CARRIER),
                  f"SF{rnd.randint(100000000000, 999999999999)}",
                  "已签收" if o[2] == "已完成" else "在途",
                  o[6][:10]) for i, o in enumerate(shipped)]
    conn.executemany("INSERT INTO shipments VALUES (?,?,?,?,?,?)", shipments)

    invoices = [(f"INV-{5000+i}", o[0], o[3], f"91320500{rnd.randint(10000000, 99999999)}",
                 o[6][:10]) for i, o in enumerate(orders[:180])]
    conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?)", invoices)

    refunds = [(f"RF-{3000+i}", o[0], round(o[3] * rnd.uniform(0.1, 1.0), 2),
                rnd.choice(_REASON), rnd.choice(["已退款", "审核中", "已驳回"]),
                o[6][:10]) for i, o in enumerate(orders[200:230])]
    conn.executemany("INSERT INTO refunds VALUES (?,?,?,?,?,?)", refunds)

    coupons = [(f"CP-{chr(65+i)}{rnd.randint(100, 999)}", rnd.choice(["满减", "折扣", "包邮"]),
                rnd.choice([10, 20, 50, 0.85, 0.9]),
                (base + timedelta(days=rnd.randint(300, 500))).strftime("%Y-%m-%d"),
                customers[rnd.randrange(60)][0]) for i in range(20)]
    conn.executemany("INSERT INTO coupons VALUES (?,?,?,?,?)", coupons)

    returns = [(f"RT-{8000+i}", o[0], skus[rnd.randrange(60)][0], rnd.randint(1, 10),
                rnd.choice(_REASON), rnd.choice(["待审核", "已入库", "已关闭"]))
               for i, o in enumerate(orders[210:235])]
    conn.executemany("INSERT INTO returns VALUES (?,?,?,?,?,?)", returns)

    reviews = [(skus[rnd.randrange(60)][0], customers[rnd.randrange(60)][0],
                rnd.randint(1, 5), rnd.choice(["质量稳定", "交期偏慢", "包装完好",
                                               "与描述一致", "希望增加规格"]))
               for _ in range(120)]
    conn.executemany("INSERT INTO reviews (sku,customer_id,rating,text) VALUES (?,?,?,?)",
                     reviews)

    contracts = [(f"CT-{400+i}", s[0], f"2026-0{rnd.randint(1, 9)}-01",
                  f"2027-0{rnd.randint(1, 9)}-01", round(rnd.uniform(5, 300) * 10000, 2))
                 for i, s in enumerate(suppliers)]
    conn.executemany("INSERT INTO contracts VALUES (?,?,?,?,?)", contracts)

    ph = []
    for s in skus[:30]:
        p = s[3]
        for k in range(3):
            old = round(p * (1 - 0.05 * (k + 1)), 2)
            new = round(p * (1 - 0.05 * k), 2)
            ph.append((s[0], (base + timedelta(days=30 * k)).strftime("%Y-%m-%d"), old, new))
    conn.executemany("INSERT INTO price_history (sku,changed_at,old_price,new_price) "
                     "VALUES (?,?,?,?)", ph)


# ---------------------------------------------------------------------------
# 只读查询：每个函数对应一个 MCP 工具
# ---------------------------------------------------------------------------
def _rows(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    conn = _connect(readonly=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _one(sql: str, args: tuple = ()) -> Optional[Dict[str, Any]]:
    r = _rows(sql + " LIMIT 1", args)
    return r[0] if r else None


def total_stock(sku: str) -> Dict[str, Any]:
    rec = _one("SELECT sku,name,category,unit_price,supplier_id FROM skus WHERE sku=?", (sku,))
    if not rec:
        return {"found": False, "sku": sku}
    inv = _rows("SELECT warehouse_id,qty,safety_stock FROM inventory WHERE sku=?", (sku,))
    total = sum(i["qty"] for i in inv)
    low = [i["warehouse_id"] for i in inv if i["qty"] < i["safety_stock"]]
    return {"found": True, **rec, "total_qty": total,
            "by_warehouse": inv, "below_safety": low}


def order_detail(order_id: str) -> Dict[str, Any]:
    o = _one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not o:
        return {"found": False, "order_id": order_id}
    items = _rows("SELECT sku,qty,price FROM order_items WHERE order_id=?", (order_id,))
    ship = _one("SELECT carrier,tracking_no,status,shipped_at FROM shipments "
                "WHERE order_id=?", (order_id,))
    inv = _one("SELECT id,issued_at FROM invoices WHERE order_id=?", (order_id,))
    rf = _rows("SELECT id,amount,reason,status FROM refunds WHERE order_id=?", (order_id,))
    return {"found": True, **o, "items": items, "shipment": ship,
            "invoice": inv, "refunds": rf}


def customer_detail(customer_id: str) -> Dict[str, Any]:
    c = _one("SELECT * FROM customers WHERE id=?", (customer_id,))
    if not c:
        return {"found": False, "customer_id": customer_id}
    os_ = _rows("SELECT id,status,amount,created_at FROM orders WHERE customer_id=? "
                "ORDER BY created_at DESC LIMIT 10", (customer_id,))
    agg = _one("SELECT COUNT(*) n, ROUND(SUM(amount),2) total FROM orders "
               "WHERE customer_id=?", (customer_id,))
    return {"found": True, **c, "recent_orders": os_,
            "order_count": (agg or {}).get("n", 0),
            "total_amount": (agg or {}).get("total", 0)}


def invoice_detail(order_id: str) -> Dict[str, Any]:
    r = _one("SELECT * FROM invoices WHERE order_id=?", (order_id,))
    return {"found": True, **r} if r else {"found": False, "order_id": order_id}


def shipment_detail(order_id: str) -> Dict[str, Any]:
    r = _one("SELECT * FROM shipments WHERE order_id=?", (order_id,))
    return {"found": True, **r} if r else {"found": False, "order_id": order_id}


def refund_detail(order_id: str) -> Dict[str, Any]:
    r = _rows("SELECT * FROM refunds WHERE order_id=?", (order_id,))
    return {"found": bool(r), "order_id": order_id, "refunds": r}


def coupon_detail(code: str) -> Dict[str, Any]:
    r = _one("SELECT * FROM coupons WHERE code=?", (code,))
    return {"found": True, **r} if r else {"found": False, "code": code}


def warehouse_detail(warehouse_id: str) -> Dict[str, Any]:
    w = _one("SELECT * FROM warehouses WHERE id=?", (warehouse_id,))
    if not w:
        return {"found": False, "warehouse_id": warehouse_id}
    agg = _one("SELECT COUNT(*) n, SUM(qty) q FROM inventory WHERE warehouse_id=?",
               (warehouse_id,))
    low = _rows("SELECT sku,qty,safety_stock FROM inventory WHERE warehouse_id=? "
                "AND qty<safety_stock LIMIT 20", (warehouse_id,))
    return {"found": True, **w, "sku_count": (agg or {}).get("n", 0),
            "total_qty": (agg or {}).get("q", 0), "below_safety": low}


def supplier_detail(supplier_id: str) -> Dict[str, Any]:
    s = _one("SELECT * FROM suppliers WHERE id=?", (supplier_id,))
    if not s:
        return {"found": False, "supplier_id": supplier_id}
    sk = _rows("SELECT sku,name,unit_price FROM skus WHERE supplier_id=? LIMIT 20",
               (supplier_id,))
    ct = _rows("SELECT id,start_date,end_date,amount FROM contracts WHERE supplier_id=?",
               (supplier_id,))
    return {"found": True, **s, "skus": sk, "contracts": ct}


def price_history(sku: str) -> Dict[str, Any]:
    r = _rows("SELECT changed_at,old_price,new_price FROM price_history WHERE sku=? "
              "ORDER BY changed_at", (sku,))
    return {"found": bool(r), "sku": sku, "history": r}


def return_detail(return_id: str) -> Dict[str, Any]:
    r = _one("SELECT * FROM returns WHERE id=?", (return_id,))
    return {"found": True, **r} if r else {"found": False, "return_id": return_id}


def review_list(sku: str) -> Dict[str, Any]:
    r = _rows("SELECT customer_id,rating,text FROM reviews WHERE sku=? LIMIT 10", (sku,))
    agg = _one("SELECT COUNT(*) n, ROUND(AVG(rating),2) avg FROM reviews WHERE sku=?", (sku,))
    return {"found": bool(r), "sku": sku, "reviews": r,
            "count": (agg or {}).get("n", 0), "avg_rating": (agg or {}).get("avg")}


def contract_detail(supplier_id: str) -> Dict[str, Any]:
    r = _rows("SELECT * FROM contracts WHERE supplier_id=?", (supplier_id,))
    return {"found": bool(r), "supplier_id": supplier_id, "contracts": r}


def employee_detail(employee_id: str) -> Dict[str, Any]:
    r = _one("SELECT * FROM employees WHERE id=?", (employee_id,))
    return {"found": True, **r} if r else {"found": False, "employee_id": employee_id}


def low_stock(threshold: int = 100) -> Dict[str, Any]:
    """缺货风险清单。

    按「单仓低于该仓安全库存」判定，而不是按全仓合计。
    全仓合计会掩盖真问题：苏州仓 0 件 + 上海仓 800 件，合计 800 看着很健康，
    但苏州的客户就是发不出货 —— 合计口径会漏报。
    """
    r = _rows("SELECT i.sku, i.warehouse_id, i.qty, i.safety_stock, s.name "
              "FROM inventory i LEFT JOIN skus s ON s.sku=i.sku "
              "WHERE i.qty < i.safety_stock ORDER BY i.qty LIMIT ?", (threshold,))
    return {"threshold": threshold, "count": len(r), "items": r}


# ---------------------------------------------------------------------------
# 写操作（唯一一个）：工单
# ---------------------------------------------------------------------------
def create_ticket(subject: str, body: str, priority: str = "P2") -> Dict[str, Any]:
    import uuid
    tid = f"TK-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?)",
                     (tid, subject, body, priority, "待处理", now))
        conn.commit()
    finally:
        conn.close()
    return {"created": True, "ticket_id": tid, "priority": priority,
            "status": "待处理", "created_at": now}


def list_tickets(limit: int = 20) -> Dict[str, Any]:
    r = _rows("SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,))
    return {"count": len(r), "tickets": r}
