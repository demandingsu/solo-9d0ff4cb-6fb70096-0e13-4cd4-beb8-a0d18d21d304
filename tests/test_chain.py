"""替代链追踪单元测试：python3 -m unittest discover -s tests -v

用本地 HTTP 服务托管样本站与临时构造的站点，直接测试 chain.trace_chain。
"""
import functools
import http.server
import pathlib
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from chain import (  # noqa: E402
    REASON_CYCLE,
    REASON_DEAD_LINK,
    REASON_MISSING_FIELDS,
    REASON_OFF_SITE,
    REASON_STATUS_UNKNOWN,
    trace_chain,
)
from scraper import ScrapeError  # noqa: E402

SITE_DIR = ROOT / "sample_site"


def start_server(directory: str):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class TestSampleSiteChain(unittest.TestCase):
    """样本站（sample_site/）上的分叉、合流、失效、环与完整链场景。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = start_server(str(SITE_DIR))

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def url(self, name: str) -> str:
        return f"{self.base}/details/{name}"

    def test_fork_merge_dead_link_and_missing_fields(self):
        # PES-45 分叉为 45A/45B；45C 是两者共同后继（合流）；
        # 另有失效链接（pes-45gone 404）与在售缺字段（pes-45d）分支
        result = trace_chain(self.url("pes-45.html"))

        # 合流节点 pes-45c 只抓取一次：共 5 个可抓取页面（gone 404 不计）
        self.assertEqual(len(result.fetched_pages), 5)
        self.assertEqual(result.fetched_pages.count(self.url("pes-45c.html")), 1)

        # 唯一的有库存在售末端是 pes-45c，且保留两条抵达路径
        self.assertEqual(len(result.terminal_paths), 2)
        paths = {tuple(p) for p in result.terminal_paths}
        self.assertEqual(
            paths,
            {
                (self.url("pes-45.html"), self.url("pes-45a.html"), self.url("pes-45c.html")),
                (self.url("pes-45.html"), self.url("pes-45b.html"), self.url("pes-45c.html")),
            },
        )
        terminal_node = result.nodes[self.url("pes-45c.html")]
        self.assertEqual(terminal_node.sale_status, "在售")
        self.assertEqual(terminal_node.stock_packs, 8)
        self.assertEqual(terminal_node.pack_size, 100)
        self.assertEqual(terminal_node.price_fen, 21000)

        # 待核实分支：失效链接 + 在售缺关键字段，其余分支不受影响
        reasons = {p.reason for p in result.pending}
        self.assertEqual(reasons, {REASON_DEAD_LINK, REASON_MISSING_FIELDS})
        dead = next(p for p in result.pending if p.reason == REASON_DEAD_LINK)
        self.assertEqual(
            dead.path,
            [self.url("pes-45.html"), self.url("pes-45b.html"), self.url("pes-45gone.html")],
        )
        missing = next(p for p in result.pending if p.reason == REASON_MISSING_FIELDS)
        self.assertEqual(
            missing.path,
            [self.url("pes-45.html"), self.url("pes-45a.html"), self.url("pes-45d.html")],
        )
        self.assertIn("可购包数", missing.message)

        # 存在待核实分支 → 结果不完整
        self.assertFalse(result.complete)

    def test_cycle_marked_pending(self):
        # PTFE-9 与 PTFE-9R 互相替代：构成环，无可采购末端
        result = trace_chain(self.url("ptfe-9.html"))
        self.assertEqual(result.terminal_paths, [])
        self.assertEqual(len(result.pending), 1)
        cycle = result.pending[0]
        self.assertEqual(cycle.reason, REASON_CYCLE)
        self.assertEqual(
            cycle.path,
            [self.url("ptfe-9.html"), self.url("ptfe-9r.html"), self.url("ptfe-9.html")],
        )
        # 环上的每个页面只抓取一次
        self.assertEqual(len(result.fetched_pages), 2)
        self.assertFalse(result.complete)

    def test_clean_chain_complete(self):
        # MCE-13 → MCE-13 Pro：单链，全部核实
        result = trace_chain(self.url("mce-13.html"))
        self.assertTrue(result.complete)
        self.assertEqual(result.pending, [])
        self.assertEqual(
            result.terminal_paths,
            [[self.url("mce-13.html"), self.url("mce-13p.html")]],
        )
        node = result.nodes[self.url("mce-13p.html")]
        self.assertEqual((node.stock_packs, node.pack_size, node.price_fen), (25, 50, 9900))

    def test_start_page_itself_on_sale(self):
        # 起始页本身就是在售末端：路径长度为 1
        result = trace_chain(self.url("pes-45c.html"))
        self.assertTrue(result.complete)
        self.assertEqual(result.terminal_paths, [[self.url("pes-45c.html")]])

    def test_start_unreachable_raises(self):
        with self.assertRaises(ScrapeError):
            trace_chain(self.url("no-such-page.html"))


class TestSyntheticChain(unittest.TestCase):
    """临时构造站点：覆盖样本站没有的边界情形。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        (self.dir / "details").mkdir()
        self.httpd, self.base = start_server(self.tmp.name)

    def tearDown(self):
        self.httpd.shutdown()
        self.tmp.cleanup()

    def write(self, name: str, html: str) -> str:
        (self.dir / "details" / name).write_text(html, encoding="utf-8")
        return f"{self.base}/details/{name}"

    @staticmethod
    def page(title, status=None, packs=None, links=(), extra_rows=""):
        rows = ""
        if status:
            rows += f'<tr><th>销售状态</th><td class="sale-status">{status}</td></tr>'
        rows += '<tr><th>每包片数</th><td class="pack-size">50</td></tr>'
        rows += '<tr><th>含税包价</th><td class="pack-price">¥10.00</td></tr>'
        if packs is not None:
            rows += f'<tr><th>可购包数</th><td class="stock-packs">{packs}</td></tr>'
        rows += extra_rows
        repl = ""
        if links:
            anchors = "".join(f'<a class="replaced-by" href="{h}">{h}</a>' for h in links)
            repl = f'<div class="replacement"><span>替代为：</span>{anchors}</div>'
        return (
            f'<html><body><h1 class="product-title">{title}</h1>'
            f'<table class="specs">{rows}</table>{repl}</body></html>'
        )

    def test_discontinued_without_successor_is_complete_dead_end(self):
        # 停产且无替代链接：已核实没有替代，不算待核实
        start = self.write("eol.html", self.page("停产无后继", status="停产", packs=0))
        result = trace_chain(start)
        self.assertTrue(result.complete)
        self.assertEqual(result.terminal_paths, [])
        self.assertEqual(result.pending, [])

    def test_offsite_successor_pending(self):
        self.write("ext.html", self.page("站外替代", status="停产", packs=0))
        start = self.write(
            "s.html",
            self.page("起始", status="停产", packs=0,
                      links=("http://example.com/details/ext.html",)),
        )
        result = trace_chain(start)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.pending), 1)
        self.assertEqual(result.pending[0].reason, REASON_OFF_SITE)
        # 站外链接未被追踪抓取
        self.assertEqual(result.fetched_pages, [start])

    def test_terminal_without_status_annotation_pending(self):
        # 末端未标注在售状态：无法核实 → 待核实
        self.write("nostatus.html", self.page("无状态标注", packs=5))
        start = self.write(
            "s.html", self.page("起始", status="停产", packs=0, links=("nostatus.html",))
        )
        result = trace_chain(start)
        self.assertFalse(result.complete)
        self.assertEqual(result.terminal_paths, [])
        self.assertEqual(result.pending[0].reason, REASON_STATUS_UNKNOWN)

    def test_on_sale_terminal_out_of_stock_listed_separately(self):
        # 在售但可购包数为 0：已核实但无货，不计入可采购末端
        self.write("oos.html", self.page("无货", status="在售", packs=0))
        start = self.write(
            "s.html", self.page("起始", status="停产", packs=0, links=("oos.html",))
        )
        result = trace_chain(start)
        self.assertTrue(result.complete)  # 无货是已核实事实，不是待核实
        self.assertEqual(result.terminal_paths, [])
        self.assertEqual(
            result.out_of_stock_paths,
            [[start, f"{self.base}/details/oos.html"]],
        )

    def test_fallback_regex_parsing(self):
        # 无 CSS 类标注：回退到文本正则解析状态与"替代为"链接
        (self.dir / "details" / "t.html").write_text(
            "<html><body><h1>替代商品</h1><p>销售状态：在售</p>"
            "<p>每包片数：30</p><p>含税包价：¥45.50</p><p>可购包数：7</p></body></html>",
            encoding="utf-8",
        )
        (self.dir / "details" / "s.html").write_text(
            '<html><body><h1>停产商品</h1><p>销售状态：停产</p>'
            '<p>每包片数：30</p><p>含税包价：¥40.00</p><p>可购包数：0</p>'
            '<p>替代为：<a href="t.html">新型号</a></p></body></html>',
            encoding="utf-8",
        )
        start = f"{self.base}/details/s.html"
        result = trace_chain(start)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.terminal_paths), 1)
        node = result.nodes[f"{self.base}/details/t.html"]
        self.assertEqual((node.sale_status, node.pack_size, node.price_fen, node.stock_packs),
                         ("在售", 30, 4550, 7))

    def test_duplicate_replaced_by_link_counted_once(self):
        # 同一页面重复列出同一替代链接：只计一次
        (self.dir / "details" / "s.html").write_text(
            '<html><body><h1>起始</h1><table><tr><td class="sale-status">停产</td></tr></table>'
            '<div class="replacement">'
            '<a class="replaced-by" href="t.html">新包装</a>'
            '<a class="replaced-by" href="t.html">新包装（重复）</a>'
            "</div></body></html>",
            encoding="utf-8",
        )
        self.write("t.html", self.page("新包装", status="在售", packs=3))
        start = f"{self.base}/details/s.html"
        result = trace_chain(start)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.terminal_paths), 1)


if __name__ == "__main__":
    unittest.main()
