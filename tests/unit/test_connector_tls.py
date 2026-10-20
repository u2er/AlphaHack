"""TLS коннекторов: корень Минцифры в наборе и докачка недостающих
промежуточных — без сети."""

from __future__ import annotations

import datetime as dt
import ssl

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

from eventgraph.connectors import tls


def test_context_trusts_russian_root_and_no_partial_chains():
    ctx = tls.ssl_context()
    subjects = [dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()]
    assert "Russian Trusted Root CA" in subjects
    assert not ctx.verify_flags & getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)


def _cert(aia: str | None) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "site.test")])
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
    )
    if aia:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [x509.AccessDescription(AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(aia))]
            ),
            critical=False,
        )
    return builder.sign(key, hashes.SHA256())


def test_ca_issuers_reads_aia():
    assert tls.ca_issuers(_cert("http://ca.test/inter.crt")) == ["http://ca.test/inter.crt"]
    assert tls.ca_issuers(_cert(None)) == []


@pytest.mark.parametrize(
    "message, expected",
    [
        ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate", True),
        ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to verify the first certificate", True),
        ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired", False),
    ],
)
def test_missing_issuer(message, expected):
    assert tls.missing_issuer(httpx.ConnectError(message)) is expected


async def test_client_completes_chain_and_retries_once_per_host(monkeypatch):
    calls: list[str] = []
    completed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "broken.test" or len(calls) == 1:
            raise httpx.ConnectError("certificate verify failed: unable to get local issuer certificate", request=request)
        return httpx.Response(200, text="ок")

    async def fake_complete(ctx, url):
        completed.append(url.host)
        return True

    monkeypatch.setattr(tls, "complete_chain", fake_complete)
    client = tls.HttpClient(transport=httpx.MockTransport(handler))

    assert (await client.get("https://site.test/")).text == "ок"
    assert calls == ["site.test", "site.test"]

    # цепочку не удалось достроить — ошибка уходит наружу, без бесконечных повторов
    with pytest.raises(httpx.ConnectError):
        await client.get("https://broken.test/")
    assert completed == ["site.test", "broken.test"]
    await client.aclose()
