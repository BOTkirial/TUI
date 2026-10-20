"""Import d'une commande `curl` (« Copier en tant que cURL » du navigateur).

Seul le format bash/POSIX est géré (Chrome « Copy as cURL (bash) », Firefox
« Copier comme cURL (POSIX) »), pas la variante Windows `cmd` avec `^`.
Les credentials (en-têtes `Authorization`, cookies…) sont conservés tels quels.
"""

from __future__ import annotations

import base64
from pathlib import PurePosixPath
from urllib.parse import urlparse

from .httpfile import HttpRequest

# en-têtes qu'on ne réécrit pas (recalculés ou dérivés de l'URL)
_SKIP_HEADERS = {"content-length", "host"}

_DATA_OPTIONS = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode",
}

# options sans effet sur la requête mais qui consomment une valeur
_IGNORED_WITH_VALUE = {
    "-o", "--output", "-m", "--max-time", "--connect-timeout", "-x", "--proxy",
    "--resolve", "--cacert", "--cert", "--key", "-w", "--write-out", "--retry",
    "-r", "--range", "-c", "--cookie-jar", "--limit-rate", "-K", "--config",
    "--interface", "-U", "--proxy-user", "--max-redirs",
}

# options courtes acceptant une valeur collée (`-XPOST`, `-H'…'`)
_SHORT_WITH_VALUE = {
    "-X", "-H", "-b", "-d", "-u", "-A", "-e", "-F",
} | {o for o in _IGNORED_WITH_VALUE if len(o) == 2}

_SIMPLE_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}


def _read_ansi_c(cmd: str, i: int) -> tuple[str, int]:
    r"""Lit une chaîne `$'…'` à partir de `i` (après `$'`), renvoie (texte, fin).

    `\xHH` et `\NNN` produisent des octets : on accumule en bytes et on décode
    en UTF-8 à la fin pour reconstituer les caractères multi-octets.
    """
    out = bytearray()
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == "'":
            return out.decode("utf-8", errors="replace"), i + 1
        if c != "\\" or i + 1 >= n:
            out += c.encode()
            i += 1
            continue
        e = cmd[i + 1]
        i += 2
        if e in _SIMPLE_ESCAPES:
            out += _SIMPLE_ESCAPES[e].encode()
        elif e in "xuU":
            size = {"x": 2, "u": 4, "U": 8}[e]
            digits = ""
            while len(digits) < size and i < n and cmd[i] in "0123456789abcdefABCDEF":
                digits += cmd[i]
                i += 1
            if not digits:
                out += ("\\" + e).encode()
            elif e == "x":
                out.append(int(digits, 16))
            else:
                out += chr(int(digits, 16)).encode()
        elif e in "01234567":
            digits = e
            while len(digits) < 3 and i < n and cmd[i] in "01234567":
                digits += cmd[i]
                i += 1
            out.append(int(digits, 8) & 0xFF)
        else:
            out += ("\\" + e).encode()
    raise ValueError("chaîne $'…' non fermée")


def tokenize(cmd: str) -> list[str]:
    """Découpe une ligne de commande comme bash (sans expansion de variables)."""
    tokens: list[str] = []
    buf: list[str] = []
    in_token = False
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        # continuation de ligne `\` + retour chariot
        if c == "\\" and cmd[i + 1:i + 3] == "\r\n":
            i += 3
            continue
        if c == "\\" and cmd[i + 1:i + 2] == "\n":
            i += 2
            continue
        if c.isspace():
            if in_token:
                tokens.append("".join(buf))
                buf, in_token = [], False
            i += 1
            continue
        in_token = True
        if c == "'":
            end = cmd.find("'", i + 1)
            if end < 0:
                raise ValueError("guillemet simple non fermé")
            buf.append(cmd[i + 1:end])
            i = end + 1
        elif c == "$" and cmd[i + 1:i + 2] == "'":
            text, i = _read_ansi_c(cmd, i + 2)
            buf.append(text)
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise ValueError("guillemet double non fermé")
                c = cmd[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n and cmd[i + 1] in '"\\$`\n':
                    if cmd[i + 1] != "\n":
                        buf.append(cmd[i + 1])
                    i += 2
                else:
                    buf.append(c)
                    i += 1
        elif c == "\\" and i + 1 < n:
            buf.append(cmd[i + 1])
            i += 2
        else:
            buf.append(c)
            i += 1
    if in_token:
        tokens.append("".join(buf))
    return tokens


def _set_header(headers: dict[str, str], raw: str) -> None:
    if ":" in raw:
        name, _, value = raw.partition(":")
        value = value.strip()
    elif raw.endswith(";"):  # `-H 'X-Vide;'` = en-tête à valeur vide pour curl
        name, value = raw[:-1], ""
    else:
        return
    name = name.strip()
    # pseudo-en-têtes HTTP/2 (`:authority`…) : name vide après partition
    if name and name.lower() not in _SKIP_HEADERS:
        headers[name] = value


def _has_header(headers: dict[str, str], name: str) -> bool:
    return any(k.lower() == name.lower() for k in headers)


def parse_curl(command: str) -> tuple[HttpRequest, list[str]]:
    """Convertit une commande curl en requête. Renvoie (requête, avertissements).

    Lève `ValueError` si la commande est illisible ou sans URL.
    """
    tokens = tokenize(command.strip())
    if not tokens or PurePosixPath(tokens[0]).name not in {"curl", "curl.exe"}:
        raise ValueError("la commande doit commencer par curl")

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    data: list[str] = []
    force_get = False
    warnings: list[str] = []

    args = iter(tokens[1:])
    for tok in args:
        value: str | None = None
        name = tok
        if tok.startswith("--") and "=" in tok:
            name, value = tok.split("=", 1)
        elif tok[:2] in _SHORT_WITH_VALUE and len(tok) > 2:
            name, value = tok[:2], tok[2:]

        def arg() -> str:
            if value is not None:
                return value
            try:
                return next(args)
            except StopIteration:
                raise ValueError(f"valeur manquante pour {name}") from None

        if not name.startswith("-") or name == "-":
            if url is None:
                url = name
        elif name == "--url":
            url = arg()
        elif name in ("-X", "--request"):
            method = arg().upper()
        elif name in ("-H", "--header"):
            _set_header(headers, arg())
        elif name in ("-b", "--cookie"):
            cookie = arg()
            if "=" in cookie:  # sinon c'est un fichier de cookies, ignoré
                old = headers.get("Cookie")
                headers["Cookie"] = f"{old}; {cookie}" if old else cookie
        elif name in _DATA_OPTIONS:
            data.append(arg())
        elif name == "--json":
            data.append(arg())
            if not _has_header(headers, "Content-Type"):
                headers["Content-Type"] = "application/json"
            if not _has_header(headers, "Accept"):
                headers["Accept"] = "application/json"
        elif name in ("-u", "--user"):
            token = base64.b64encode(arg().encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        elif name in ("-A", "--user-agent"):
            headers["User-Agent"] = arg()
        elif name in ("-e", "--referer"):
            headers["Referer"] = arg()
        elif name in ("-G", "--get"):
            force_get = True
        elif name in ("-I", "--head"):
            method = "HEAD"
        elif name in ("-F", "--form"):
            arg()
            warnings.append("formulaire multipart (-F) ignoré")
        elif name in _IGNORED_WITH_VALUE:
            arg()
        # autres options (--compressed, -k, -sSL…) : sans effet sur la requête

    if not url:
        raise ValueError("aucune URL trouvée dans la commande")

    body = "&".join(data)
    if force_get and body:
        url += ("&" if "?" in url else "?") + body
        body = ""
    if body and not _has_header(headers, "Content-Type"):
        # comportement de curl pour -d sans Content-Type explicite
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if any(d.startswith("@") for d in data):
        warnings.append("données lues depuis un fichier (@…) non résolues")

    if method is None:
        method = "GET" if force_get or not body else "POST"
    return HttpRequest(method=method, url=url, headers=headers, body=body), warnings


def request_name(req: HttpRequest) -> str:
    """Nom par défaut : dernier segment du chemin, ou l'hôte (la méthode est
    déjà affichée dans l'arbre)."""
    parsed = urlparse(req.url)
    return PurePosixPath(parsed.path).name or parsed.netloc or "requête"
