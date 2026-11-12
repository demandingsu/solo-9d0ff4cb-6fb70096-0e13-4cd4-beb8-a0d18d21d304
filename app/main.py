"""滤膜采购组合优化 API（FastAPI）。

POST /api/v1/quote
  输入：目录 URL、滤膜型号、需求片数
  输出：库存内总价最低的整包组合（各包装采购包数、实到片数、总价、详情来源）

POST /api/v1/replacement-chain
  输入：停产商品详情页 URL
  输出：沿"替代为"链接追踪到的有库存在售末端包装、替代路径与来源；
        环 / 失效链接 / 在售缺关键字段等分支标记待核实，存在待核实分支时
        complete=false（结果不完整，但已核实分支仍返回）

报价 API 状态语义：
  ok                  找到确定的最优组合
  insufficient_stock  库存总量不足，不给出确定最低价
  scrape_failed       匹配详情页抓取/解析失败，不给出确定最低价
  no_matching_products 目录中没有匹配型号的商品
  catalog_unreachable 目录列表页抓取失败

替代链 API 状态语义：
  ok                替代链追踪完成，所有分支已核实（complete=true）
  partial           存在待核实分支，已核实分支仍返回但结果不完整（complete=false）
  start_unreachable 起始详情页抓取失败
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field

from chain import ChainNode, ChainResult, trace_chain
from optimizer import Offer, optimize
from scraper import ScrapeError, crawl_catalog

app = FastAPI(title="滤膜采购组合优化 API", version="1.1.0")


class QuoteRequest(BaseModel):
    catalog_url: str = Field(..., description="供应商目录列表页 URL")
    model: str = Field(..., min_length=1, description="滤膜型号，如 MF-50")
    demand: int = Field(..., ge=1, description="需求片数")


class ReplacementChainRequest(BaseModel):
    detail_url: str = Field(..., min_length=1, description="停产商品详情页 URL")


def fen_to_yuan(fen: int) -> str:
    sign = "-" if fen < 0 else ""
    fen = abs(fen)
    return f"{sign}{fen // 100}.{fen % 100:02d}"


@app.get("/health")
def health():
    return {"status": "up"}


@app.post("/api/v1/quote")
def quote(req: QuoteRequest):
    try:
        crawl = crawl_catalog(req.catalog_url, req.model)
    except ScrapeError as exc:
        return {
            "status": "catalog_unreachable",
            "message": f"目录列表页抓取失败：{exc}",
            "model": req.model,
            "demand": req.demand,
            "solution": None,
        }

    base = {
        "model": req.model,
        "demand": req.demand,
        "list_pages": crawl.list_pages,
        "matched_detail_pages": [o.detail_url for o in crawl.matched],
        "duplicate_detail_links_skipped": crawl.duplicate_details,
    }

    # 匹配详情抓取失败：不给出确定的最低价
    if crawl.failed_details:
        return {
            **base,
            "status": "scrape_failed",
            "message": "部分匹配详情页抓取或解析失败，无法给出确定的最低价",
            "failed_detail_pages": crawl.failed_details,
            "solution": None,
        }

    if not crawl.matched:
        resp = {
            **base,
            "status": "no_matching_products",
            "message": "目录中未找到匹配该型号的商品",
            "solution": None,
        }
        if crawl.unverified_failed:
            resp["message"] += "（另有部分详情页无法抓取，未能核对）"
            resp["unverified_failed_pages"] = crawl.unverified_failed
        return resp

    # 兜底扫描时有详情页未能核对：可能漏掉更优的匹配，不给确定最低价
    if crawl.unverified_failed:
        return {
            **base,
            "status": "scrape_failed",
            "message": "部分详情页无法抓取、未能核对是否还有更优匹配，无法给出确定的最低价",
            "failed_detail_pages": crawl.unverified_failed,
            "solution": None,
        }

    offers = [Offer(o.pack_size, o.price_fen, o.stock_packs) for o in crawl.matched]
    total_available = sum(o.pack_size * o.stock_packs for o in offers)

    try:
        plan = optimize(offers, req.demand)
    except ValueError as exc:
        return {
            **base,
            "status": "demand_too_large",
            "message": str(exc),
            "total_available_pieces": total_available,
            "solution": None,
        }

    # 库存不足：不给出确定的最低价
    if plan is None:
        return {
            **base,
            "status": "insufficient_stock",
            "message": f"库存不足：可购总量 {total_available} 片 < 需求 {req.demand} 片，无法给出确定的最低价",
            "total_available_pieces": total_available,
            "solution": None,
        }

    items = []
    for info, packs in zip(crawl.matched, plan.packs):
        if packs == 0:
            continue
        items.append(
            {
                "detail_url": info.detail_url,
                "title": info.title,
                "model": info.model,
                "pack_size": info.pack_size,
                "pack_price": fen_to_yuan(info.price_fen),
                "stock_packs": info.stock_packs,
                "purchased_packs": packs,
                "delivered_pieces": packs * info.pack_size,
                "subtotal_price": fen_to_yuan(packs * info.price_fen),
            }
        )

    return {
        **base,
        "status": "ok",
        "message": "已找到库存内总价最低的整包组合",
        "solution": {
            "items": items,
            "total_pieces": plan.total_pieces,
            "demand": req.demand,
            "overage": plan.total_pieces - req.demand,
            "total_price": fen_to_yuan(plan.total_price_fen),
            "currency": "CNY",
        },
        "total_available_pieces": total_available,
    }


def _node_summary(node: ChainNode) -> dict:
    return {
        "detail_url": node.detail_url,
        "title": node.title,
        "model": node.model,
        "sale_status": node.sale_status,
    }


def _terminal_entry(node: ChainNode, paths: list[list[str]]) -> dict:
    """一个有库存在售末端包装：包装字段、来源详情 URL 与全部替代路径。"""
    return {
        **_node_summary(node),
        "pack_size": node.pack_size,
        "pack_price": fen_to_yuan(node.price_fen),  # type: ignore[arg-type]
        "stock_packs": node.stock_packs,
        "paths": paths,
    }


def _group_paths(result: ChainResult, paths: list[list[str]]) -> list[dict]:
    """按末端详情分组路径：同一末端只抓取一次，但保留抵达它的各条路径。"""
    by_terminal: dict[str, list[list[str]]] = {}
    for path in paths:
        by_terminal.setdefault(path[-1], []).append(path)
    return [
        _terminal_entry(result.nodes[url], group)
        for url, group in by_terminal.items()
    ]


@app.post("/api/v1/replacement-chain")
def replacement_chain(req: ReplacementChainRequest):
    try:
        result = trace_chain(req.detail_url)
    except ScrapeError as exc:
        return {
            "status": "start_unreachable",
            "complete": False,
            "message": f"起始详情页抓取失败：{exc}",
            "start_url": req.detail_url,
            "start": None,
            "terminals": [],
            "out_of_stock_terminals": [],
            "pending_branches": [],
            "fetched_pages": [],
            "fetch_count": 0,
        }

    terminals = _group_paths(result, result.terminal_paths)
    out_of_stock = _group_paths(result, result.out_of_stock_paths)
    pending = [
        {"reason": p.reason, "path": p.path, "message": p.message}
        for p in result.pending
    ]
    complete = result.complete

    if not complete:
        message = (
            f"存在 {len(pending)} 条待核实分支，结果不完整；"
            "已核实的分支如下，但可能还有其他可采购替代包装未确认"
        )
    elif terminals:
        message = f"替代链追踪完成，所有分支已核实，找到 {len(terminals)} 种有库存的在售末端包装"
    else:
        message = "替代链追踪完成，所有分支已核实，但未找到有库存的在售替代包装"

    return {
        "status": "ok" if complete else "partial",
        "complete": complete,
        "message": message,
        "start_url": result.start_url,
        "start": _node_summary(result.nodes[result.start_url]),
        "terminals": terminals,
        "out_of_stock_terminals": out_of_stock,
        "pending_branches": pending,
        "fetched_pages": result.fetched_pages,
        "fetch_count": len(result.fetched_pages),
    }
