"""Names shared by the listener and its clients. Importable without Django."""
from typing import Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

PAIR_ALPN = b'smartscope/pair/1'
HTTP_ALPN = b'smartscope/http/1'


def pairing_link(ticket: str, code: str) -> str:
    """What a user hands to a device: where the listener is and a one-time code."""
    return 'smartscope://pair?' + urlencode({'ticket': ticket, 'code': code})


def parse_pairing_link(link: str) -> Tuple[str, str]:
    query = parse_qs(urlsplit(link.strip()).query)
    return query['ticket'][0], query['code'][0]
