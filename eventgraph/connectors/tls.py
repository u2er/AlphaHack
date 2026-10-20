from __future__ import annotations

import logging
import ssl
from pathlib import Path

import certifi
import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

logger = logging.getLogger(__name__)

CERTS_DIR = Path(__file__).resolve().parent / "certs"
# Цепочки длиннее не бывают: лист → промежуточный → (ещё один) → корень.
MAX_DEPTH = 3
MAX_CERT_BYTES = 64 * 1024
_MISSING_ISSUER = ("unable to get local issuer certificate", "unable to verify the first certificate")

# Докачанные промежуточные: ссылка → PEM. Общие для всех запусков воркера.
_intermediates: dict[str, str] = {}


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    # С 3.13 частичные цепочки включены по умолчанию — тогда докачанный
    # промежуточный стал бы якорем доверия сам по себе.
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    for pem in sorted(CERTS_DIR.glob("*.pem")):
        ctx.load_verify_locations(cafile=str(pem))
    for pem in list(_intermediates.values()):
        ctx.load_verify_locations(cadata=pem)
    return ctx


def missing_issuer(exc: BaseException) -> bool:
    text = str(exc)
    return any(marker in text for marker in _MISSING_ISSUER)


def _load(data: bytes) -> x509.Certificate:
    if data.lstrip().startswith(b"-----BEGIN"):
        return x509.load_pem_x509_certificate(data)
    return x509.load_der_x509_certificate(data)


def ca_issuers(cert: x509.Certificate) -> list[str]:
    """Ссылки на сертификат издателя из расширения AIA."""
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return []
    return [
        d.access_location.value
        for d in aia
        if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
        and isinstance(d.access_location, x509.UniformResourceIdentifier)
    ]


async def _leaf(probe: httpx.AsyncClient, url: httpx.URL) -> x509.Certificate | None:
    """Сертификат сервера — без проверки: нужно лишь прочитать, кто его выдал."""
    async with probe.stream("GET", url) as response:
        stream = response.extensions.get("network_stream")
        ssl_object = stream.get_extra_info("ssl_object") if stream is not None else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    return x509.load_der_x509_certificate(der) if der else None


async def complete_chain(ctx: ssl.SSLContext, url: httpx.URL) -> bool:
    """Докачивает недостающих издателей сертификата `url` в `ctx`.

    True — что-то добавилось и запрос стоит повторить.
    """
    added = False
    # Без проверки и через тот же прокси из окружения, что и основной клиент.
    async with httpx.AsyncClient(verify=False, timeout=15, follow_redirects=False) as probe:
        try:
            cert = await _leaf(probe, url)
        except httpx.HTTPError as exc:
            logger.info("не прочитал сертификат %s: %s", url.host, exc)
            return False
        for _ in range(MAX_DEPTH):
            if cert is None or cert.issuer == cert.subject:
                break
            links = ca_issuers(cert)
            if not links:
                break
            link = links[0]
            pem = _intermediates.get(link)
            if pem is None:
                try:
                    response = await probe.get(link, follow_redirects=True)
                    response.raise_for_status()
                    if len(response.content) > MAX_CERT_BYTES:
                        break
                    issuer = _load(response.content)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.info("не докачал издателя %s по %s: %s", url.host, link, exc)
                    break
                pem = issuer.public_bytes(Encoding.PEM).decode()
                _intermediates[link] = pem
                logger.info("%s: докачан промежуточный сертификат %s", url.host, issuer.subject.rfc4514_string())
            else:
                issuer = _load(pem.encode())
            ctx.load_verify_locations(cadata=pem)
            added = True
            cert = issuer
    return added


class HttpClient(httpx.AsyncClient):
    """`ctx.http`: AsyncClient, который сам достраивает неполные цепочки."""

    def __init__(self, **kwargs) -> None:
        self._ssl = ssl_context()
        self._completed: set[str] = set()
        super().__init__(verify=self._ssl, **kwargs)

    async def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        for _ in range(MAX_DEPTH):
            try:
                return await super().send(request, **kwargs)
            except httpx.ConnectError as exc:
                # Редирект мог увести на другой хост — чиним тот, где упало.
                try:
                    failed = exc.request.url
                except RuntimeError:
                    failed = request.url
                if not missing_issuer(exc) or failed.host in self._completed:
                    raise
                self._completed.add(failed.host)
                if not await complete_chain(self._ssl, failed):
                    raise
        return await super().send(request, **kwargs)
