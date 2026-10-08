"""联网搜索：那几个**能离线验证**的环节。

背景：线上表现为「搜什么都搜不到」。查下来根因在网络（本机出口是机房 IP，
搜索引擎要么扔 CAPTCHA、要么喂随机垃圾），但排查过程中挖出好几个**代码本身
的 bug** —— 它们让任何一次搜索都注定失败。这个文件锁住这些环节。

不联网：只测纯函数（链接解包、跳转壳识别、拦截页识别）。真正的搜索要发请求，
放进来会让测试依赖外部网络，那才是不可靠的测试。
"""
from __future__ import annotations

import base64

from bs4 import BeautifulSoup

from web_search import WebSearch


class TestUnwrapBingRedirect:
    """Bing 的结果链接是跳转地址，直接抓只会拿到一个空壳中转页。

    真实表现：把 `www.bing.com/ck/a?...` 当正文页去抓，拿回来的是
    「Please click here if the page does not redirect automatically」——
    正文一个字都没有，等于没搜。
    """

    @staticmethod
    def _bing_link(real: str) -> str:
        payload = "a1" + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
        return f"https://www.bing.com/ck/a?!&&p=deadbeef&u={payload}&ntb=1"

    def test_it_recovers_the_real_url(self):
        real = "https://zh.wikipedia.org/wiki/中國"
        assert WebSearch._unwrap_link(self._bing_link(real)) == real

    def test_it_handles_missing_padding(self):
        """Bing 的 base64 去掉了 `=` 补位，必须自己补回来。"""
        real = "https://baike.baidu.com/item/中华人民共和国/106"
        link = self._bing_link(real)
        assert "=" not in link.split("u=")[1].split("&")[0]
        assert WebSearch._unwrap_link(link) == real

    def test_ordinary_links_pass_through(self):
        for url in ("https://example.com/a", "https://www.so.com/link?m=abc"):
            assert WebSearch._unwrap_link(url) == url

    def test_garbage_does_not_crash(self):
        assert WebSearch._unwrap_link("https://www.bing.com/ck/a?u=!!!!") .startswith("http")


class TestRealUrlFromShell:
    """360 的链接是 `so.com/link?m=...`，返回一个 280 字节的 JS 跳转壳。

    requests 不执行 JS，所以会停在壳上、正文为空。真地址就写在脚本里。
    """

    SHELL = (
        '<meta content="always" name="referrer">'
        '<script>window.location.replace("https://www.runoob.com/docker/docker-tutorial.html")</script>'
        '<noscript><meta http-equiv="refresh" content="0;URL=\'https://www.runoob.com/docker/docker-tutorial.html\'"></noscript>'
    )

    def test_it_reads_the_location_replace_script(self):
        assert WebSearch._real_url_from_shell(self.SHELL) == \
            "https://www.runoob.com/docker/docker-tutorial.html"

    def test_it_falls_back_to_the_meta_refresh(self):
        html = '<meta http-equiv="refresh" content="0;URL=\'https://example.com/x\'">'
        assert WebSearch._real_url_from_shell(html) == "https://example.com/x"

    def test_a_normal_page_yields_nothing(self):
        assert WebSearch._real_url_from_shell("<html><body>普通文章</body></html>") == ""


class TestRedirectStubGuard:
    """抓回来的如果是跳转空壳，宁可不要，也别喂给 AI。"""

    def test_it_rejects_the_bing_stub(self):
        stub = "Please\nclick here\nif the page does not redirect automatically ..."
        assert WebSearch._is_redirect_stub(stub) is True

    def test_it_rejects_chinese_stubs(self):
        assert WebSearch._is_redirect_stub("页面跳转中，请稍候") is True

    def test_it_rejects_near_empty_pages(self):
        assert WebSearch._is_redirect_stub("  ") is True

    def test_real_content_survives(self):
        text = "Docker 是一个开源的应用容器引擎，让开发者可以打包应用以及依赖包到一个可移植的镜像中。" * 3
        assert WebSearch._is_redirect_stub(text) is False


class TestBlockDetection:
    """「被反爬拦了」必须和「真没搜到」分开 —— 否则用户会一直换关键词重试。"""

    def _resp(self, status: int, body: str):
        class R:
            status_code = status
            text = body
        return R()

    DDG_CHALLENGE = (
        "<html><head><title>DuckDuckGo</title></head><body>"
        "Unfortunately, bots use DuckDuckGo too. Please complete the following "
        "challenge to confirm this search was made by a human. Select all squares "
        "containing a duck:</body></html>"
    )

    def test_ddg_captcha_is_detected(self):
        w = WebSearch()
        soup = BeautifulSoup(self.DDG_CHALLENGE, "lxml")
        assert w._looks_blocked(self._resp(202, self.DDG_CHALLENGE), soup) is True

    def test_rate_limit_status_is_detected(self):
        w = WebSearch()
        soup = BeautifulSoup("<html><body>ok</body></html>", "lxml")
        assert w._looks_blocked(self._resp(429, "<html><body>ok</body></html>"), soup) is True

    def test_a_normal_page_is_not_flagged(self):
        w = WebSearch()
        html = "<html><head><title>Docker 教程 - 菜鸟教程</title></head><body><p>Docker 是一个容器引擎。</p></body></html>"
        assert w._looks_blocked(self._resp(200, html), BeautifulSoup(html, "lxml")) is False

    def test_block_words_inside_scripts_do_not_false_positive(self):
        """整页 HTML 里出现 challenge/captcha 这种词不算 —— 脚本里就有。

        只看标题和开头可见正文，否则正常的搜索结果页会被误判成拦截页。
        """
        w = WebSearch()
        html = (
            "<html><head><title>搜索结果</title></head><body><p>正常内容</p>"
            "<script>var challengeMode = false; /* captcha */</script></body></html>"
        )
        assert w._looks_blocked(self._resp(200, html), BeautifulSoup(html, "lxml")) is False


class TestSearchReasonContract:
    def test_reason_defaults_to_ok(self):
        assert WebSearch().last_reason == "ok"

    def test_duckduckgo_is_kept_as_a_last_resort(self):
        """DDG 对机房 IP 一律 CAPTCHA，但删掉它就少了一道保险。"""
        assert WebSearch.PROVIDERS[-1] == "duckduckgo"

    def test_wikipedia_is_in_the_chain(self):
        """scraping 系全被喂垃圾时，维基百科的 API 是唯一稳定的来源。"""
        assert "wikipedia" in WebSearch.PROVIDERS
