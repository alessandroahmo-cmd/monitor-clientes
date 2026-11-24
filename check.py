#!/usr/bin/env python3
"""Verificador de sites: lê links_clientes.txt, consulta cada link via HTTP
e gera site/data/status.json para o painel semáforo.

Só usa a biblioteca padrão do Python (nada para instalar).

Variáveis de ambiente opcionais:
  PREVIOUS_STATUS_URL  URL do status.json publicado (para manter histórico e alertas)
  TELEGRAM_TOKEN       token do bot do Telegram
  TELEGRAM_CHAT_ID     id do chat/grupo que recebe os alertas
  PANEL_URL            link do painel, incluído nos alertas
"""
import concurrent.futures
import datetime as dt
import html
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
LINKS_FILE = os.path.join(ROOT, "links_clientes.txt")
OUT_FILE = os.path.join(ROOT, "site", "data", "status.json")

TIMEOUT = 20            # segundos para considerar o site fora
SLOW_MS = 3000          # acima disso: amarelo (lento)
SSL_WARN_DAYS = 15      # certificado vencendo em menos de X dias: amarelo
HISTORY_SIZE = 288      # 288 checagens de 5 min = 24 h
FAILS_TO_ALERT = 2      # checagens vermelhas seguidas antes de alertar (evita alarme falso)
USER_AGENT = "Mozilla/5.0 (compatible; MonitorClientes/1.0)"


def parse_links(path):
    """Lê blocos 'CLIENTE X' seguidos de URLs. Linhas que não são URL são ignoradas."""
    sites, client = [], "Sem cliente"
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"(?i)^cliente\s*[:\-]?\s*(.+)$", line)
            if m:
                client = m.group(1).strip().upper()
                continue
            if re.match(r"(?i)^https?://", line):
                sites.append({"client": client, "url": line})
    return sites


def environment_of(url):
    host = urllib.parse.urlparse(url).hostname or ""
    labels = re.split(r"[.\-]", host)
    if any(l in ("hom", "hmg", "homolog", "homologacao") or l.endswith("hmg") for l in labels):
        return "Homologação"
    return "Produção"


def ssl_expiry(host, port=443):
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as s:
                cert = s.getpeercert()
        exp = dt.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
        issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "")
        return {"valid": True, "expires": exp.isoformat(), "days_left": (exp - dt.datetime.now(dt.timezone.utc)).days, "issuer": issuer}
    except ssl.SSLCertVerificationError as e:
        return {"valid": False, "error": f"Certificado inválido: {e.verify_message}"}
    except Exception as e:  # noqa: BLE001
        return {"valid": None, "error": str(e)[:150]}


def page_title(body):
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:120]


def page_icon(body, base_url):
    for m in re.finditer(r"<link\b[^>]*>", body, re.I):
        tag = m.group(0)
        if re.search(r'rel=["\'][^"\']*icon', tag, re.I):
            h = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if h:
                return urllib.parse.urljoin(base_url, html.unescape(h.group(1)))
    return urllib.parse.urljoin(base_url, "/favicon.ico")


def check(site):
    url = site["url"]
    r = {
        **site,
        "env": environment_of(url),
        "host": urllib.parse.urlparse(url).hostname,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    start = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(300_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
            r["http"] = resp.status
            r["final_url"] = resp.geturl()
    except urllib.error.HTTPError as e:
        r["http"] = e.code
        r["final_url"] = e.geturl() if hasattr(e, "geturl") else url
        body = ""
        try:
            body = e.read(300_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            r["error"] = f"Certificado SSL inválido: {reason.verify_message}"
        elif isinstance(reason, socket.gaierror):
            r["error"] = "Endereço não encontrado (DNS)"
        elif isinstance(reason, (socket.timeout, TimeoutError)):
            r["error"] = f"Sem resposta em {TIMEOUT}s"
        elif isinstance(reason, ConnectionRefusedError):
            r["error"] = "Conexão recusada"
        else:
            r["error"] = str(reason)[:150]
        body = ""
    except (socket.timeout, TimeoutError):
        r["error"] = f"Sem resposta em {TIMEOUT}s"
        body = ""
    except Exception as e:  # noqa: BLE001
        r["error"] = str(e)[:150]
        body = ""
    r["ms"] = round((time.monotonic() - start) * 1000)

    if body:
        r["title"] = page_title(body)
        r["icon"] = page_icon(body, r.get("final_url") or url)

    if url.lower().startswith("https://"):
        r["ssl"] = ssl_expiry(r["host"])

    # ---- semáforo ----
    reasons, color = [], "green"
    http = r.get("http")
    if "error" in r:
        color, reasons = "red", [r["error"]]
    elif http and http >= 500:
        color, reasons = "red", [f"Erro no servidor (HTTP {http})"]
    elif http in (404, 410):
        color, reasons = "red", [f"Página não encontrada (HTTP {http})"]
    elif http and http >= 400:
        color, reasons = "yellow", [f"Respondeu com HTTP {http}"]
    else:
        if http != 200:
            color = "yellow"
            reasons.append(f"Respondeu com HTTP {http}")
        if r["ms"] > SLOW_MS:
            color = "yellow"
            reasons.append(f"Lento ({r['ms'] / 1000:.1f}s)")
        final_host = urllib.parse.urlparse(r.get("final_url") or url).hostname
        if final_host and final_host != r["host"]:
            color = "yellow"
            reasons.append(f"Redirecionou para {final_host}")
    s = r.get("ssl") or {}
    if s.get("valid") is False:
        color = "red"
        reasons.append(s.get("error", "Certificado inválido"))
    elif s.get("days_left") is not None:
        if s["days_left"] < 0:
            color = "red"
            reasons.append("Certificado SSL vencido")
        elif s["days_left"] < SSL_WARN_DAYS and color == "green":
            color = "yellow"
            reasons.append(f"Certificado vence em {s['days_left']} dias")
        elif s["days_left"] < SSL_WARN_DAYS:
            reasons.append(f"Certificado vence em {s['days_left']} dias")
    r["color"] = color
    r["reasons"] = reasons or ["Funcionando normalmente"]
    return r


def load_previous():
    src = os.environ.get("PREVIOUS_STATUS_URL", "").strip()
    try:
        if src:
            req = urllib.request.Request(f"{src}?t={int(time.time())}", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        if os.path.exists(OUT_FILE):
            with open(OUT_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: não consegui ler o status anterior ({e}); começando do zero.", file=sys.stderr)
    return {}


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"Falha ao enviar Telegram: {e}", file=sys.stderr)


def fmt_duration(seconds):
    m = int(seconds // 60)
    if m < 60:
        return f"{m} min"
    return f"{m // 60}h{m % 60:02d}"


def main():
    sites = parse_links(LINKS_FILE)
    if not sites:
        sys.exit("Nenhum link encontrado em links_clientes.txt")
    prev = {s["url"]: s for s in load_previous().get("sites", [])}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(check, sites))

    now = dt.datetime.now(dt.timezone.utc)
    panel = os.environ.get("PANEL_URL", "").strip()
    down_msgs, up_msgs = [], []
    for r in results:
        p = prev.get(r["url"], {})
        hist = (p.get("history") or [])[-(HISTORY_SIZE - 1):]
        hist.append({"t": r["checked_at"], "c": r["color"][0], "ms": r["ms"]})
        r["history"] = hist
        r["uptime_24h"] = round(100 * sum(h["c"] != "r" for h in hist) / len(hist), 1)

        r["fail_streak"] = p.get("fail_streak", 0) + 1 if r["color"] == "red" else 0
        r["alerted"] = p.get("alerted", False)
        r["down_since"] = p.get("down_since") if r["color"] == "red" else None
        if r["color"] == "red" and not r["down_since"]:
            r["down_since"] = r["checked_at"]

        label = f"<b>{html.escape(r['client'])}</b> ({r['env']})\n{html.escape(r['url'])}"
        if r["color"] == "red" and r["fail_streak"] >= FAILS_TO_ALERT and not r["alerted"]:
            down_msgs.append(f"🔴 {label}\nMotivo: {html.escape('; '.join(r['reasons']))}")
            r["alerted"] = True
        elif r["color"] != "red" and r["alerted"]:
            since = p.get("down_since")
            dur = ""
            if since:
                dur = f"\nFicou fora por ~{fmt_duration((now - dt.datetime.fromisoformat(since)).total_seconds())}"
            up_msgs.append(f"🟢 {label}\nVoltou a responder (HTTP {r.get('http')}, {r['ms']} ms){dur}")
            r["alerted"] = False

    if down_msgs:
        telegram("⚠️ <b>Site(s) fora do ar</b>\n\n" + "\n\n".join(down_msgs) + (f"\n\nPainel: {panel}" if panel else ""))
    if up_msgs:
        telegram("✅ <b>Site(s) normalizado(s)</b>\n\n" + "\n\n".join(up_msgs) + (f"\n\nPainel: {panel}" if panel else ""))

    counts = {c: sum(r["color"] == c for r in results) for c in ("green", "yellow", "red")}
    out = {"generated_at": now.isoformat(timespec="seconds"), "counts": counts, "sites": results}
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    for r in results:
        print(f"{r['color']:6} {r.get('http', '---')!s:>4} {r['ms']:>6} ms  {r['url']}  {'; '.join(r['reasons'])}")
    print(f"\nVerde: {counts['green']}  Amarelo: {counts['yellow']}  Vermelho: {counts['red']}")


if __name__ == "__main__":
    main()
