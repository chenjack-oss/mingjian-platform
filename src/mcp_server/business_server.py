"""
业务系统 MCP Server（stdio）

为什么单独起进程：
    MCP 的全部意义就是"工具提供方和使用方不在同一个进程"。
    如果业务查询和 Agent 在同一进程里，那就是普通函数调用，挂 MCP 只是多了层开销。
    这里业务库由本进程独占访问，平台只通过协议拿结果 —— 换 Java 写的 ERP 也照样接。

工具集（15 个 = 14 只读 + 1 写）：
    核心 3 个：query_order / query_stock / create_ticket
    干扰 12 个：customer / invoice / shipment / refund / coupon / warehouse /
               supplier / price_history / return / review / contract / employee /
               low_stock
    干扰工具不是桩函数，全部是同一份数据集上的真实查询，否则准确率实验没有意义。

写操作二次确认（create_ticket）：
    第一次调用不落库，返回 need_confirm + confirm_token；
    用户口头确认后，模型带 token 再调一次才真正写入。
    token 绑定**参数指纹**（subject+body+priority 的哈希）——
    不绑定的话，用户看着 A 工单点了确认，模型拿同一个 token 去建 B 工单，确认就形同虚设。

运行：
    python -m src.mcp_server.business_server
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from typing import Any, Dict, Optional

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from src.business import store  # noqa: E402
from src.observe.logging_setup import configure_logging, get_logger  # noqa: E402

# ⚠️ 必须输出到 stderr：本进程的 stdout 是 JSON-RPC 协议通道，
# 往 stdout 写一个字符都会破坏帧结构，客户端只会看到 "Connection closed"，
# 真正的错误反而被吞掉。这是 stdio 形态 MCP server 的固定约束。
configure_logging(stream=sys.stderr)
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 二次确认令牌（进程内存，仅本进程有效）
# ---------------------------------------------------------------------------
# TTL 300s：太短用户还没读完就过期，太长等于长期授权。
_TOKEN_TTL = int(os.getenv("MJ_MCP_CONFIRM_TTL", "300"))
_tokens: Dict[str, Dict[str, Any]] = {}
_seq = [0]


def _fingerprint(subject: str, body: str, priority: str) -> str:
    raw = f"{subject}||{body}||{priority}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _new_token(fp: str, preview: Dict[str, Any]) -> str:
    _seq[0] += 1
    tok = f"cf_{int(time.time())}_{_seq[0]}"
    _tokens[tok] = {"fp": fp, "preview": preview, "exp": time.time() + _TOKEN_TTL}
    _cleanup()
    return tok


def _check_token(tok: str, fp: str) -> str:
    """返回 "" 表示通过，否则返回拒绝原因。"""
    rec = _tokens.get(tok)
    if not rec:
        return "confirm_token 无效或已过期，请重新发起并向用户确认"
    if time.time() > rec["exp"]:
        _tokens.pop(tok, None)
        return "confirm_token 已过期，请重新发起并向用户确认"
    if rec["fp"] != fp:
        # 参数被改过：说明确认的和要建的不是同一件事，必须重新确认
        return ("confirm_token 与当前参数不匹配（工单内容已变更），请重新发起并向用户确认")
    _tokens.pop(tok, None)
    return ""


def _cleanup() -> None:
    now = time.time()
    for k in [k for k, v in _tokens.items() if now > v["exp"]]:
        _tokens.pop(k, None)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
CORE_TOOLS = {"query_order", "query_stock", "create_ticket"}

_server = MCPServer(
    name="mingjian-business",
    instructions=(
        "企业业务系统（订单/库存/客户/物流/财务）只读查询与工单创建。"
        "除 create_ticket 外全部为只读；create_ticket 必须先取得用户明确确认才能执行。"
    ),
)


def _maybe_register(name: str):
    """按 MJ_MCP_TOOLSET 决定是否注册。core=只挂 3 个，all=全挂。"""
    ts = os.getenv("MJ_MCP_TOOLSET", "all").strip().lower()
    if ts == "core" and name not in CORE_TOOLS:
        return lambda fn: fn
    return _server.tool(
        name=name,
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )


def _ro(fn):
    return _maybe_register(fn.__name__)(fn)


# --- 核心 3 个 -------------------------------------------------------------
@_server.tool(
    name="query_order",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
def query_order(order_id: str) -> Dict[str, Any]:
    """按订单号查询订单详情：状态、金额、支付方式、发货仓、商品明细、物流与发票。

    适用于用户给出具体订单号（形如 SO-900000）并想了解该订单情况时。
    只查订单相关；查库存用 query_stock，查客户累计消费用 query_customer。
    """
    return store.order_detail(order_id.strip())


@_server.tool(
    name="query_stock",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
def query_stock(sku: str) -> Dict[str, Any]:
    """按 SKU 查询库存：全仓合计数量、分仓库存、是否低于安全库存。

    适用于用户问"某个货号还有多少货/能不能发货"。
    查整仓缺货清单用 low_stock；查某个订单用 query_order。
    """
    return store.total_stock(sku.strip())


@_server.tool(
    name="create_ticket",
    annotations={"readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False},
)
def create_ticket(subject: str, body: str, priority: str = "P2",
                  confirm_token: str = "") -> Dict[str, Any]:
    """创建售后/异常工单（**写操作，必须先取得用户明确确认**）。

    调用协议（两步）：
      1. 不带 confirm_token 调用 → 返回 need_confirm=True 与 confirm_token，
         以及将要写入的内容预览。**此时不会创建任何工单**。
      2. 向用户展示预览并得到明确确认（如"确认""可以""建吧"）后，
         带上同一个 confirm_token 再调用一次 → 真正创建并返回工单号。

    硬性要求：未得到用户确认不得自作主张带 token 调用；用户说"算了/不用了"则放弃。
    priority 取 P0/P1/P2/P3，未指定用 P2。
    """
    fp = _fingerprint(subject, body, priority)
    if not confirm_token:
        tok = _new_token(fp, {"subject": subject, "body": body, "priority": priority})
        return {
            "need_confirm": True,
            "confirm_token": tok,
            "preview": {"subject": subject, "body": body, "priority": priority},
            "message": ("尚未创建工单。请先把上述预览展示给用户，取得明确确认后，"
                        "带上 confirm_token 再次调用本工具。"),
        }
    deny = _check_token(confirm_token, fp)
    if deny:
        return {"need_confirm": True, "created": False, "message": deny}
    rec = store.create_ticket(subject, body, priority)
    return {"need_confirm": False, "created": True, **rec}


# --- 干扰 12 个（真实查询，非桩）-------------------------------------------
@_ro
def query_customer(customer_id: str) -> Dict[str, Any]:
    """按客户编号查询客户档案：等级、城市、累计订单数与累计金额、最近 10 笔订单。

    适用于问"这个客户是谁/买了多少"。查单笔订单明细用 query_order。
    """
    return store.customer_detail(customer_id.strip())


@_ro
def query_invoice(order_id: str) -> Dict[str, Any]:
    """按订单号查询发票：发票号、金额、税号、开票日期。

    只回答发票本身；订单状态与明细用 query_order。
    """
    return store.invoice_detail(order_id.strip())


@_ro
def query_shipment(order_id: str) -> Dict[str, Any]:
    """按订单号查询物流：承运商、运单号、在途/已签收、发货日期。

    只回答物流；货款与退款分别用 query_invoice / query_refund。
    """
    return store.shipment_detail(order_id.strip())


@_ro
def query_refund(order_id: str) -> Dict[str, Any]:
    """按订单号查询退款记录：退款金额、原因、退款状态。

    只回答退款；想看订单整体情况用 query_order。
    """
    return store.refund_detail(order_id.strip())


@_ro
def query_coupon(code: str) -> Dict[str, Any]:
    """按优惠码查询优惠券：类型（满减/折扣/包邮）、面值、有效期、使用人。

    与订单/库存无关，不要用它回答货物类问题。
    """
    return store.coupon_detail(code.strip())


@_ro
def query_warehouse(warehouse_id: str) -> Dict[str, Any]:
    """按仓库编号查询仓库：容量、在库 SKU 数、总库存、低于安全库存的 SKU 清单。

    问"某仓整体情况"用这个；问"某个货号有多少"用 query_stock。
    """
    return store.warehouse_detail(warehouse_id.strip())


@_ro
def query_supplier(supplier_id: str) -> Dict[str, Any]:
    """按供应商编号查询供应商：联系人、交货周期、供应的 SKU 列表与合同。

    问"谁供的货/交期多久"用这个；问"还有多少货"用 query_stock。
    """
    return store.supplier_detail(supplier_id.strip())


@_ro
def query_price_history(sku: str) -> Dict[str, Any]:
    """按 SKU 查询历史调价记录：调价日期、原价、新价。

    只回答价格变动；当前库存用 query_stock。
    """
    return store.price_history(sku.strip())


@_ro
def query_return(return_id: str) -> Dict[str, Any]:
    """按退货单号查询退货单：关联订单、SKU、数量、原因、处理状态。

    退货单号形如 RT-8000。想建工单用 create_ticket。
    """
    return store.return_detail(return_id.strip())


@_ro
def query_review(sku: str) -> Dict[str, Any]:
    """按 SKU 查询客户评价：评分、评价内容、平均分与条数。

    只回答口碑评价；库存与订单用 query_stock / query_order。
    """
    return store.review_list(sku.strip())


@_ro
def query_contract(supplier_id: str) -> Dict[str, Any]:
    """按供应商编号查询采购合同：合同号、起止日期、合同金额。

    只回答合同；供应商主体信息用 query_supplier。
    """
    return store.contract_detail(supplier_id.strip())


@_ro
def query_employee(employee_id: str) -> Dict[str, Any]:
    """按员工编号查询员工：姓名、部门、职务、邮箱。

    只回答人员信息，不要用它与业务单据混答。
    """
    return store.employee_detail(employee_id.strip())


@_ro
def low_stock(limit: int = 20) -> Dict[str, Any]:
    """列出存在缺货风险的 SKU：单仓库存低于该仓安全库存的条目。

    问"哪些货要补了"用这个；问"某个货号具体多少件"用 query_stock。
    """
    return store.low_stock(max(1, min(int(limit), 100)))


def build_server() -> MCPServer:
    return _server


async def _serve() -> None:
    # 踩坑：MCPServer.list_tools() 是 **async**，按同步调用只会拿到协程对象，
    # 拿 len() 直接 TypeError，且错误发生在子进程里，客户端只看到 "Connection closed"，
    # 很难定位 —— 这也是为什么服务端启动日志必须打全。
    store.ensure_seeded()
    names = [t.name for t in await _server.list_tools()]
    # 已由 configure_logging(stream=sys.stderr) 保证不污染协议通道
    logger.info("已注册 %d 个工具: %s", len(names), ", ".join(names))
    await _server.run_stdio_async()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
