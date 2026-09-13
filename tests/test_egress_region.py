import pytest

from deepdesk.egress_region import EgressRegionDetector


@pytest.mark.asyncio
async def test_cloudflare_country_is_parsed_without_retaining_ip(monkeypatch):
    class Response:
        content = b"ip=203.0.113.4\nloc=CN\n"
        text = content.decode()

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return Response()

    monkeypatch.setattr("deepdesk.egress_region.httpx.AsyncClient", lambda **_kwargs: Client())
    result = await EgressRegionDetector().detect()

    assert result.country_code == "CN"
    assert result.is_mainland_china is True
    assert not hasattr(result, "ip")


@pytest.mark.asyncio
async def test_detector_does_not_cache_between_tasks(monkeypatch):
    countries = iter(("CN", "US"))

    class Response:
        content = b""

        def __init__(self):
            self.text = f"loc={next(countries)}\n"

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return Response()

    monkeypatch.setattr("deepdesk.egress_region.httpx.AsyncClient", lambda **_kwargs: Client())
    detector = EgressRegionDetector()

    assert (await detector.detect()).country_code == "CN"
    assert (await detector.detect()).country_code == "US"
