"""替代链追踪：从停产详情页沿"替代为"同站链接逐级追踪仍可采购的替代包装。

追踪规则：
- 从起始详情页出发，沿页面标注的"替代为"链接逐级追踪；一个商品允许有多个后继（分叉）；
- 只跟随与起始页同站（同 host）的链接；站外替代链接不追踪，对应分支标记待核实；
- 每个详情 URL 全局只抓取一次；多条路径抵达同一详情（合流）时保留各条路径；
- 没有"替代为"链接的详情页为末端：
  - 在售、关键包装字段（每包片数/含税包价/可购包数）齐全且可购包数 > 0
    → 有库存的在售末端包装（已核实）；
  - 在售但缺少关键包装字段 → 该分支待核实（missing_fields）；
  - 在售但可购包数为 0 → 已核实但暂时无货，单独列出（不计入可采购末端）；
  - 停产且无替代链接 → 已核实的死胡同（确认无替代，不算待核实）；
  - 未标注或无法识别在售状态 → 该分支待核实（status_unknown）；
- 检测到环（cycle）、替代链接失效（dead_link）、抓取页数超限（limit_exceeded）
  → 受影响分支待核实；
- 只要存在待核实分支，结果就不完整（complete=False）：已核实分支仍返回，
  但调用方不得宣称结果完整。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from scraper import (
    USER_AGENT,
    ScrapeError,
    _abs_url,
    _get_soup,
    _pick_text,
    _same_site,
    extract_detail_fields,
)

MAX_CHAIN_PAGES = 50  # 单次追踪抓取的详情页上限，防止死循环或异常站点

# "替代为"链接的 CSS 选择器，按优先级依次尝试
REPLACED_BY_SELECTORS = [
    "a.replaced-by",
    ".replaced-by a[href]",
    ".replacement a[href]",
    "#replacement a[href]",
    '[data-field="replaced-by"] a[href]',
]
REPLACED_TEXT_RE = re.compile(r"替代")
STATUS_SELECTORS = [".sale-status", '[data-field="status"]', ".product-status"]
STATUS_RE = re.compile(r"(?:销售)?状态\s*[:：]?\s*(在售|停产|下架)")

ON_SALE = "在售"
DISCONTINUED = "停产"

# 待核实原因
REASON_CYCLE = "cycle"                # 替代链出现环
REASON_DEAD_LINK = "dead_link"        # 替代链接抓取失败（404、超时等）
REASON_MISSING_FIELDS = "missing_fields"    # 在售详情缺少关键包装字段
REASON_STATUS_UNKNOWN = "status_unknown"    # 页面未标注在售状态，无法核实
REASON_OFF_SITE = "off_site"          # 替代链接指向站外，未追踪
REASON_LIMIT_EXCEEDED = "limit_exceeded"    # 抓取页数超限，分支未追踪


@dataclass
class ChainNode:
    """一个详情页解析出的替代链信息。"""

    detail_url: str
    title: str
    model: str
    sale_status: str | None        # "在售" / "停产" / None（未标注或无法识别）
    pack_size: int | None          # 每包片数
    price_fen: int | None          # 含税包价（分）
    stock_packs: int | None        # 可购包数
    successors: list[str]          # 同站"替代为"链接（去重、保序）
    offsite_successors: list[str]  # 站外"替代为"链接（不追踪）


@dataclass
class PendingBranch:
    """一条待核实的分支：从起始页到问题点的完整路径 + 原因。"""

    reason: str
    path: list[str]
    message: str


@dataclass
class ChainResult:
    """替代链追踪结果。"""

    start_url: str
    nodes: dict[str, ChainNode]         # 实际抓取到的详情页（每个 URL 只抓一次）
    terminal_paths: list[list[str]]     # 以"有库存的在售末端"结尾的路径
    out_of_stock_paths: list[list[str]]  # 以"在售但无货末端"结尾的路径（已核实）
    pending: list[PendingBranch]        # 待核实分支
    fetched_pages: list[str]            # 实际抓取的详情页 URL（按抓取顺序）

    @property
    def complete(self) -> bool:
        """没有待核实分支时结果才完整。"""
        return not self.pending


def _parse_sale_status(soup: BeautifulSoup, page_text: str) -> str | None:
    """解析页面标注的在售状态：返回 "在售" / "停产" / None（未标注或无法识别）。"""
    raw = _pick_text(soup, STATUS_SELECTORS)
    if not raw:
        m = STATUS_RE.search(page_text)
        raw = m.group(1) if m else ""
    if ON_SALE in raw:
        return ON_SALE
    if "停产" in raw or "下架" in raw:
        return DISCONTINUED
    return None


def _replaced_by_links(soup: BeautifulSoup) -> list[str]:
    """提取页面中的"替代为"链接 href：优先 CSS 选择器，回退到链接文本含"替代"。"""
    for sel in REPLACED_BY_SELECTORS:
        links = [a for a in soup.select(sel) if a.get("href")]
        if links:
            return [a["href"] for a in links]
    # 回退：锚文本或其父元素文本含"替代"（如 <p>替代为：<a>产品名</a></p>）
    found = []
    for a in soup.find_all("a", href=True):
        if REPLACED_TEXT_RE.search(a.get_text(strip=True)):
            found.append(a["href"])
        elif a.parent and REPLACED_TEXT_RE.search(a.parent.get_text(" ", strip=True)):
            found.append(a["href"])
    return found


def parse_chain_page(url: str, soup: BeautifulSoup, netloc: str) -> ChainNode:
    """解析详情页的在售状态、包装字段与"替代为"链接（宽松解析，不抛错）。"""
    page_text = soup.get_text("\n", strip=True)
    fields = extract_detail_fields(soup)
    sale_status = _parse_sale_status(soup, page_text)

    successors: list[str] = []
    offsite: list[str] = []
    seen: set[str] = set()
    for href in _replaced_by_links(soup):
        abs_url = _abs_url(href, url)
        if abs_url in seen:
            continue  # 同一页面重复列出的替代链接只计一次
        seen.add(abs_url)
        if _same_site(abs_url, netloc):
            successors.append(abs_url)
        else:
            offsite.append(abs_url)

    return ChainNode(
        detail_url=url,
        title=fields.title,
        model=fields.model,
        sale_status=sale_status,
        pack_size=fields.pack_size,
        price_fen=fields.price_fen,
        stock_packs=fields.stock_packs,
        successors=successors,
        offsite_successors=offsite,
    )


def trace_chain(start_url: str) -> ChainResult:
    """从起始详情页追踪替代链。起始页抓取失败时抛 ScrapeError。"""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    netloc = urlparse(start_url).netloc

    nodes: dict[str, ChainNode] = {}
    fetched: list[str] = []
    failed: set[str] = set()
    _BUDGET = object()  # 抓取页数超限的哨兵

    def get_node(url: str):
        """抓取并解析详情页；每个 URL 全局只抓一次。失败返回 None，超限返回哨兵。"""
        if url in nodes:
            return nodes[url]
        if url in failed:
            return None
        if len(fetched) >= MAX_CHAIN_PAGES:
            return _BUDGET
        try:
            soup = _get_soup(session, url)
            node = parse_chain_page(url, soup, netloc)
        except ScrapeError:
            failed.add(url)
            return None
        nodes[url] = node
        fetched.append(url)
        return node

    start = get_node(start_url)
    if start is None or start is _BUDGET:
        raise ScrapeError(f"起始详情页抓取失败 {start_url}")

    # memo[url] = (terminal_paths, out_of_stock_paths, pendings)，路径均以 url 开头；
    # 只有完全探索完的节点才写入 memo，因此合流节点只展开一次、各路径分别接续。
    memo: dict[str, tuple[list[list[str]], list[list[str]], list[PendingBranch]]] = {}
    in_stack: set[str] = set()  # 当前 DFS 路径上的节点，用于检测环

    def explore(url: str):
        if url in memo:
            return memo[url]
        in_stack.add(url)
        node = nodes[url]
        terms: list[list[str]] = []
        ooss: list[list[str]] = []
        pens: list[PendingBranch] = []

        if not node.successors and not node.offsite_successors:
            # 末端节点：按页面标注的在售状态与包装字段判定
            if node.sale_status == ON_SALE:
                missing = [
                    name
                    for name, val in [
                        ("每包片数", node.pack_size),
                        ("含税包价", node.price_fen),
                        ("可购包数", node.stock_packs),
                    ]
                    if val is None
                ]
                if missing:
                    pens.append(PendingBranch(
                        REASON_MISSING_FIELDS, [url],
                        f"在售详情缺少关键字段：{'、'.join(missing)}",
                    ))
                elif node.stock_packs and node.stock_packs > 0:
                    terms.append([url])  # 有库存的在售末端包装
                else:
                    ooss.append([url])   # 在售但无货：已核实，单独列出
            elif node.sale_status == DISCONTINUED:
                pass  # 停产且无替代链接：已核实没有替代包装
            else:
                pens.append(PendingBranch(
                    REASON_STATUS_UNKNOWN, [url],
                    "页面未标注在售状态，无法核实是否可采购",
                ))
        else:
            for succ in node.successors:
                if succ in in_stack:
                    pens.append(PendingBranch(
                        REASON_CYCLE, [url, succ],
                        f"检测到替代链环：{succ} 在当前路径中已出现",
                    ))
                    continue
                child = get_node(succ)
                if child is _BUDGET:
                    pens.append(PendingBranch(
                        REASON_LIMIT_EXCEEDED, [url, succ],
                        f"抓取页数超过上限 {MAX_CHAIN_PAGES}，该分支未追踪",
                    ))
                    continue
                if child is None:
                    pens.append(PendingBranch(
                        REASON_DEAD_LINK, [url, succ],
                        f"替代链接抓取失败：{succ}",
                    ))
                    continue
                sub_terms, sub_ooss, sub_pens = explore(succ)
                terms.extend([url] + p for p in sub_terms)
                ooss.extend([url] + p for p in sub_ooss)
                pens.extend(PendingBranch(p.reason, [url] + p.path, p.message) for p in sub_pens)
            for succ in node.offsite_successors:
                pens.append(PendingBranch(
                    REASON_OFF_SITE, [url, succ],
                    f"替代链接指向站外，未追踪：{succ}",
                ))

        in_stack.discard(url)
        memo[url] = (terms, ooss, pens)
        return memo[url]

    terms, ooss, pens = explore(start_url)
    return ChainResult(
        start_url=start_url,
        nodes=nodes,
        terminal_paths=terms,
        out_of_stock_paths=ooss,
        pending=pens,
        fetched_pages=fetched,
    )
