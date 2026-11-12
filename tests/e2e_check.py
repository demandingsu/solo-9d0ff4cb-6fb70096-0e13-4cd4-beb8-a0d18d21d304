"""端到端检查：本地 HTTP 服务托管样本站，调用 FastAPI 完整链路。

运行：python3 tests/e2e_check.py
"""
import functools
import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402

SITE_DIR = ROOT / "sample_site"


def start_site_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def check(name: str, cond: bool, extra: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        raise SystemExit(f"检查失败: {name}")


def main():
    httpd, base = start_site_server()
    client = TestClient(app)
    catalog = f"{base}/index.html"
    print(f"样本站: {catalog}")

    # 1) 正常求解：MF-50 需求 180 → 2 包 100 片装，¥450
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 180})
    assert r.status_code == 200, r.text
    d = r.json()
    check("MF-50/180 状态 ok", d["status"] == "ok")
    check("总价 ¥450.00", d["solution"]["total_price"] == "450.00")
    check("实到 200 片", d["solution"]["total_pieces"] == 200)
    check("超量 20 片", d["solution"]["overage"] == 20)
    items = d["solution"]["items"]
    check("仅一种包装", len(items) == 1)
    check("100片装 ×2", items[0]["pack_size"] == 100 and items[0]["purchased_packs"] == 2)
    check("详情来源 URL", items[0]["detail_url"].endswith("/details/mf50-100.html"))

    # 2) 分页与跨页去重：3 个列表页都被抓取，mf50-50 只计一次
    check("抓取 3 个列表页", len(d["list_pages"]) == 3, str(d["list_pages"]))
    check("跨页重复跳过 1 次", d["duplicate_detail_links_skipped"] == 1)
    check("匹配详情 3 个", len(d["matched_detail_pages"]) == 3, str(d["matched_detail_pages"]))

    # 3) 混购 + 库存约束：需求 175 → 100+50+25 各 1 包，¥413
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 175})
    d = r.json()
    check("MF-50/175 状态 ok", d["status"] == "ok")
    check("混购 3 种包装", len(d["solution"]["items"]) == 3)
    check("总价 ¥413.00", d["solution"]["total_price"] == "413.00")
    check("实到 175 片（零超量）", d["solution"]["total_pieces"] == 175 and d["solution"]["overage"] == 0)

    # 4) 库存上限：需求 1000 → 恰好买光 10 包 100 片装
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 1000})
    d = r.json()
    check("MF-50/1000 状态 ok", d["status"] == "ok")
    check("总价 ¥2250.00", d["solution"]["total_price"] == "2250.00")
    check("实到 1000 片", d["solution"]["total_pieces"] == 1000)

    # 5) 库存不足：需求 4000 > 可购总量 3750 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "MF-50", "demand": 4000})
    d = r.json()
    check("MF-50/4000 状态 insufficient_stock", d["status"] == "insufficient_stock")
    check("无方案、无总价", d["solution"] is None)
    check("报告可购总量 3750", d["total_available_pieces"] == 3750)

    # 6) 匹配详情抓取失败：BROKEN-1 的详情页 404 → 不给确定最低价
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "BROKEN-1", "demand": 10})
    d = r.json()
    check("BROKEN-1 状态 scrape_failed", d["status"] == "scrape_failed")
    check("无方案", d["solution"] is None)
    check("报告失败详情页", d["failed_detail_pages"][0].endswith("/details/broken-404.html"))

    # 7) 无匹配型号（兜底扫描时 broken-404 无法核对，应在响应中如实标注）
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NOPE-9", "demand": 10})
    d = r.json()
    check("NOPE-9 状态 no_matching_products", d["status"] == "no_matching_products")
    check("无方案", d["solution"] is None)
    check(
        "标注未能核对的页面",
        any(u.endswith("/details/broken-404.html") for u in d.get("unverified_failed_pages", [])),
    )

    # 8) 其他型号正常匹配：NY-66 需求 150 → 2 包 100 片装 ¥360
    r = client.post("/api/v1/quote", json={"catalog_url": catalog, "model": "NY-66", "demand": 150})
    d = r.json()
    check("NY-66/150 状态 ok", d["status"] == "ok")
    check("总价 ¥360.00", d["solution"]["total_price"] == "360.00")

    # 9) 目录不可达
    r = client.post("/api/v1/quote", json={"catalog_url": f"{base}/no-such-page.html", "model": "MF-50", "demand": 10})
    d = r.json()
    check("目录不存在状态 catalog_unreachable", d["status"] == "catalog_unreachable")

    # 10) 兜底扫描语义：列表文案不含型号时逐个核对详情页
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        (tmp_path / "details").mkdir()
        (tmp_path / "index.html").write_text(
            '<html><body><ul class="product-list">'
            '<li class="product-item"><a class="product-link" href="details/d1.html">特价滤膜 甲</a></li>'
            '<li class="product-item"><a class="product-link" href="details/d2.html">特价滤膜 乙</a></li>'
            "</ul></body></html>",
            encoding="utf-8",
        )
        (tmp_path / "details" / "d1.html").write_text(
            '<html><body><h1 class="product-title">特价滤膜 甲</h1>'
            '<table><tr><td class="product-model">FALL-1</td>'
            '<td class="pack-size">40</td>'
            '<td class="pack-price">¥90.00</td>'
            '<td class="stock-packs">8</td></tr></table></body></html>',
            encoding="utf-8",
        )
        # d2.html 故意不存在（404）
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
        httpd2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd2.serve_forever, daemon=True).start()
        cat2 = f"http://127.0.0.1:{httpd2.server_address[1]}/index.html"

        # 10a) 详情页型号匹配（FALL-1 只在详情页出现）→ 命中，但 d2 未能核对 → 不给确定最低价
        r = client.post("/api/v1/quote", json={"catalog_url": cat2, "model": "FALL-1", "demand": 40})
        d = r.json()
        check("兜底命中但存在未核对页 → scrape_failed", d["status"] == "scrape_failed")
        check("无方案", d["solution"] is None)
        check("失败页为 d2", d["failed_detail_pages"][0].endswith("/details/d2.html"))

        # 10b) 全部不匹配 → no_matching_products 并标注未核对页
        r = client.post("/api/v1/quote", json={"catalog_url": cat2, "model": "ABSENT-7", "demand": 40})
        d = r.json()
        check("兜底无匹配 → no_matching_products", d["status"] == "no_matching_products")
        check("标注未核对页 d2", any(u.endswith("/details/d2.html") for u in d.get("unverified_failed_pages", [])))
        httpd2.shutdown()

    # 11) 替代链：分叉 + 合流 + 失效链接 + 在售缺字段（PES-45 家族）
    r = client.post("/api/v1/replacement-chain", json={"detail_url": f"{base}/details/pes-45.html"})
    assert r.status_code == 200, r.text
    d = r.json()
    check("PES-45 状态 partial（存在待核实分支）", d["status"] == "partial")
    check("PES-45 结果不宣称完整", d["complete"] is False)
    check("起始商品标注停产", d["start"]["sale_status"] == "停产")
    check("唯一可采购末端 pes-45c", len(d["terminals"]) == 1)
    t = d["terminals"][0]
    check("末端为 pes-45c", t["detail_url"].endswith("/details/pes-45c.html"))
    check("末端在售、8 包库存", t["sale_status"] == "在售" and t["stock_packs"] == 8)
    check("末端 100 片 ¥210.00", t["pack_size"] == 100 and t["pack_price"] == "210.00")
    check("合流保留 2 条路径", len(t["paths"]) == 2, str(t["paths"]))
    check(
        "路径分别经过 pes-45a / pes-45b",
        {p[1].rsplit("/", 1)[-1] for p in t["paths"]} == {"pes-45a.html", "pes-45b.html"},
    )
    check("合流节点只抓取一次（共 5 页）", d["fetch_count"] == 5, str(d["fetched_pages"]))
    check(
        "fetched_pages 中 pes-45c 仅一次",
        sum(u.endswith("/details/pes-45c.html") for u in d["fetched_pages"]) == 1,
    )
    reasons = {p["reason"] for p in d["pending_branches"]}
    check("待核实原因 = 失效链接 + 缺字段", reasons == {"dead_link", "missing_fields"}, str(reasons))
    dead = next(p for p in d["pending_branches"] if p["reason"] == "dead_link")
    check("失效分支路径指向 pes-45gone", dead["path"][-1].endswith("/details/pes-45gone.html"))
    miss = next(p for p in d["pending_branches"] if p["reason"] == "missing_fields")
    check("缺字段分支路径指向 pes-45d", miss["path"][-1].endswith("/details/pes-45d.html"))
    check("缺字段消息注明可购包数", "可购包数" in miss["message"])

    # 12) 替代链：环（PTFE-9 ↔ PTFE-9R）
    r = client.post("/api/v1/replacement-chain", json={"detail_url": f"{base}/details/ptfe-9.html"})
    d = r.json()
    check("PTFE-9 状态 partial", d["status"] == "partial")
    check("环场景无可采购末端", d["terminals"] == [])
    check("环上 2 页各抓一次", d["fetch_count"] == 2)
    check("报告环待核实", d["pending_branches"][0]["reason"] == "cycle")
    check(
        "环路径首尾同为 ptfe-9",
        d["pending_branches"][0]["path"][0].endswith("/details/ptfe-9.html")
        and d["pending_branches"][0]["path"][-1].endswith("/details/ptfe-9.html"),
    )

    # 13) 替代链：完整单链（MCE-13 → MCE-13 Pro）
    r = client.post("/api/v1/replacement-chain", json={"detail_url": f"{base}/details/mce-13.html"})
    d = r.json()
    check("MCE-13 状态 ok", d["status"] == "ok")
    check("MCE-13 结果完整", d["complete"] is True)
    check("无待核实分支", d["pending_branches"] == [])
    check("唯一末端 mce-13p", len(d["terminals"]) == 1)
    t = d["terminals"][0]
    check("mce-13p 50 片 ¥99.00 ×25 包", (t["pack_size"], t["pack_price"], t["stock_packs"]) == (50, "99.00", 25))
    check("替代路径 mce-13 → mce-13p", [u.rsplit("/", 1)[-1] for u in t["paths"][0]] == ["mce-13.html", "mce-13p.html"])

    # 14) 替代链：起始详情页不存在
    r = client.post("/api/v1/replacement-chain", json={"detail_url": f"{base}/details/no-such.html"})
    d = r.json()
    check("起始页 404 状态 start_unreachable", d["status"] == "start_unreachable")
    check("起始页 404 无末端", d["terminals"] == [] and d["complete"] is False)

    httpd.shutdown()
    print("\n全部端到端检查通过 ✓")


if __name__ == "__main__":
    main()
