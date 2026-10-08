from services.price_tracker import _price_from_html


def test_price_from_visible_text():
    assert _price_from_html('<div class="item__price-once">14 390 ₸</div>') == 14390


def test_price_from_meta_and_jsonld():
    assert _price_from_html('<meta property="product:price:amount" content="14390">') == 14390
    assert _price_from_html('{"offers": {"@type":"Offer","price": "11990"}}') == 11990
    assert _price_from_html('{"lowPrice": 15290}') == 15290


def test_price_missing():
    assert _price_from_html("<html></html>") == 0.0
