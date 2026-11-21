from jarvis_books.text import linkify


def test_bare_and_markdown_links_become_anchors():
    out = linkify("Bids at https://www.govease.com/auctions. Or [Zeus](https://www.zeusauction.com/) now")
    assert out == ('Bids at <a href="https://www.govease.com/auctions" style="color:#63b3ed">'
                   'https://www.govease.com/auctions</a>. Or <a href="https://www.zeusauction.com/" '
                   'style="color:#63b3ed">Zeus</a> now')


def test_www_links_get_https_and_brackets_are_kept_outside():
    out = linkify("(see www.bid4assets.com/county-tax-sales)")
    assert out.startswith('(see <a href="https://www.bid4assets.com/county-tax-sales"') and out.endswith("</a>)")


def test_text_is_escaped_and_lines_kept():
    out = linkify('<script>x</script>\nhttps://a.gov/?q="1"&b=2')
    assert "<script>" not in out and "&lt;script&gt;" in out and "<br>" in out
    assert '<a href="https://a.gov/?q=" style="color:#63b3ed">https://a.gov/?q=</a>&quot;1&quot;' in out


def test_no_links_is_plain_escaped_text():
    assert linkify("Tom & Jerry") == "Tom &amp; Jerry" and linkify("") == ""
