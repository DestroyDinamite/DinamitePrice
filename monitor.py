#!/usr/bin/env python3
"""
Monitor de Preços — avisa quando o preço de um produto muda.

Uso:
    python monitor.py add <link> [--nome "Nome"] [--seletor "css"]
    python monitor.py list
    python monitor.py remove <id>
    python monitor.py check            # confere todos uma vez
    python monitor.py run --minutos 60 # fica rodando e confere a cada N minutos
    python monitor.py test-notify      # testa as notificações configuradas
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ARQUIVO = Path(os.environ.get("MONITOR_ARQUIVO", Path(__file__).with_name("produtos.json")))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# Seletores conhecidos de lojas populares (usados se não houver dados estruturados)
SELETORES_CONHECIDOS = [
    'meta[itemprop="price"]',
    '[itemprop="price"]',
    ".ui-pdp-price__second-line .andes-money-amount",   # Mercado Livre
    ".andes-money-amount",                              # Mercado Livre
    "#corePrice_feature_div .a-offscreen",              # Amazon
    ".a-price .a-offscreen",                            # Amazon
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    '[data-testid="price-value"]',
    '[data-testid="price"]',
    ".price-tag",
    ".product-price",
    ".price",
]


# --------------------------------------------------------------------------- #
# Extração de preço
# --------------------------------------------------------------------------- #
def parse_preco(texto) -> float | None:
    """Converte '1.299,90', 'R$ 1.299,90', '1299.9', '1,299.90' em float."""
    if texto is None:
        return None
    if isinstance(texto, (int, float)):
        return float(texto)
    m = re.search(r"\d[\d.,\s]*", str(texto).replace("\xa0", " "))
    if not m:
        return None
    num = re.sub(r"\s", "", m.group(0)).rstrip(".,")
    if "," in num and "." in num:
        # o último separador é o decimal
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        inteiro, _, dec = num.rpartition(",")
        num = (inteiro.replace(",", "") + "." + dec) if len(dec) <= 2 else num.replace(",", "")
    elif num.count(".") >= 1:
        inteiro, _, dec = num.rpartition(".")
        if len(dec) == 3:  # '1.299' -> milhar
            num = num.replace(".", "")
        else:
            num = inteiro.replace(".", "") + "." + dec
    try:
        valor = float(num)
    except ValueError:
        return None
    return valor if valor > 0 else None


def _precos_jsonld(soup: BeautifulSoup) -> list[float]:
    precos = []

    def visitar(obj):
        if isinstance(obj, list):
            for o in obj:
                visitar(o)
        elif isinstance(obj, dict):
            for chave in ("price", "lowPrice"):
                if chave in obj:
                    p = parse_preco(obj[chave])
                    if p:
                        precos.append(p)
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    visitar(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            visitar(json.loads(tag.string or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return precos


def _titulo(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return "Produto"


def extrair_preco(html: str, seletor: str | None = None) -> tuple[float | None, str]:
    """Retorna (preço, título) a partir do HTML da página."""
    soup = BeautifulSoup(html, "html.parser")
    titulo = _titulo(soup)

    # 1) seletor CSS informado pelo usuário
    if seletor:
        el = soup.select_one(seletor)
        if el:
            return parse_preco(el.get("content") or el.get_text(" ", strip=True)), titulo
        return None, titulo

    # 2) dados estruturados (JSON-LD schema.org)
    precos = _precos_jsonld(soup)
    if precos:
        return precos[0], titulo

    # 3) meta tags de preço
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "twitter:data1"},
    ):
        meta = soup.find("meta", attrs=attrs)
        if meta and parse_preco(meta.get("content")):
            return parse_preco(meta.get("content")), titulo

    # 4) seletores conhecidos
    for sel in SELETORES_CONHECIDOS:
        el = soup.select_one(sel)
        if el:
            p = parse_preco(el.get("content") or el.get_text(" ", strip=True))
            if p:
                return p, titulo

    return None, titulo


def buscar(url: str, seletor: str | None = None) -> tuple[float | None, str]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return extrair_preco(resp.text, seletor)


# --------------------------------------------------------------------------- #
# Notificações
# --------------------------------------------------------------------------- #
def notificar(titulo: str, mensagem: str, link: str | None = None) -> None:
    print(f"\n🔔 {titulo}\n{mensagem}\n")
    enviados = []

    # ntfy.sh — push grátis no celular, sem cadastro (app "ntfy" na loja)
    topico = os.environ.get("NTFY_TOPICO")
    if topico:
        try:
            headers = {"Title": titulo.encode("utf-8"), "Tags": "moneybag"}
            if link:
                headers["Click"] = link
            servidor = os.environ.get("NTFY_SERVIDOR", "https://ntfy.sh").rstrip("/")
            requests.post(f"{servidor}/{topico}", data=mensagem.encode("utf-8"),
                          headers=headers, timeout=15).raise_for_status()
            enviados.append("ntfy")
        except Exception as e:
            print(f"  ⚠ ntfy falhou: {e}")

    # Telegram
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            texto = f"*{titulo}*\n{mensagem}" + (f"\n{link}" if link else "")
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": texto, "parse_mode": "Markdown"},
                          timeout=15).raise_for_status()
            enviados.append("telegram")
        except Exception as e:
            print(f"  ⚠ Telegram falhou: {e}")

    # E-mail (SMTP — ex.: Gmail com "senha de app")
    host, usuario = os.environ.get("SMTP_HOST"), os.environ.get("SMTP_USUARIO")
    if host and usuario:
        try:
            msg = EmailMessage()
            msg["Subject"] = titulo
            msg["From"] = usuario
            msg["To"] = os.environ.get("EMAIL_PARA", usuario)
            msg.set_content(mensagem + (f"\n\n{link}" if link else ""))
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORTA", "587")), timeout=30) as s:
                s.starttls()
                s.login(usuario, os.environ["SMTP_SENHA"])
                s.send_message(msg)
            enviados.append("email")
        except Exception as e:
            print(f"  ⚠ E-mail falhou: {e}")

    if not enviados:
        print("  (nenhum canal de notificação configurado — veja o README)")


def brl(v: float | None) -> str:
    if v is None:
        return "—"
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------- #
# Armazenamento
# --------------------------------------------------------------------------- #
def carregar() -> list[dict]:
    if ARQUIVO.exists():
        return json.loads(ARQUIVO.read_text(encoding="utf-8"))
    return []


def salvar(produtos: list[dict]) -> None:
    ARQUIVO.write_text(json.dumps(produtos, ensure_ascii=False, indent=2), encoding="utf-8")


def agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def cmd_add(args) -> None:
    produtos = carregar()
    if any(p["url"] == args.url for p in produtos):
        sys.exit("Esse link já está sendo monitorado.")
    print("Buscando preço atual...")
    try:
        preco, titulo = buscar(args.url, args.seletor)
    except requests.RequestException as e:
        sys.exit(f"Não consegui abrir a página: {e}")
    if preco is None:
        print("⚠ Não encontrei o preço automaticamente. Ele será salvo mesmo assim;\n"
              "  se continuar sem preço, use --seletor com o seletor CSS do preço.")
    novo_id = max((p["id"] for p in produtos), default=0) + 1
    produtos.append({
        "id": novo_id,
        "nome": args.nome or titulo[:80],
        "url": args.url,
        "seletor": args.seletor,
        "preco": preco,
        "menor_preco": preco,
        "historico": [{"data": agora(), "preco": preco}] if preco else [],
    })
    salvar(produtos)
    print(f"✔ [{novo_id}] {args.nome or titulo[:80]} — {brl(preco)}")


def cmd_list(_args) -> None:
    produtos = carregar()
    if not produtos:
        print("Nenhum produto. Adicione com: python monitor.py add <link>")
        return
    for p in produtos:
        print(f"[{p['id']}] {p['nome']}\n    atual: {brl(p['preco'])}   menor: "
              f"{brl(p.get('menor_preco'))}\n    {p['url']}")


def cmd_remove(args) -> None:
    produtos = carregar()
    restantes = [p for p in produtos if p["id"] != args.id]
    if len(restantes) == len(produtos):
        sys.exit(f"Id {args.id} não encontrado.")
    salvar(restantes)
    print(f"✔ Removido {args.id}")


def checar_todos() -> None:
    produtos = carregar()
    if not produtos:
        print("Nenhum produto para checar.")
        return
    for p in produtos:
        try:
            novo, titulo = buscar(p["url"], p.get("seletor"))
            if not p.get("nome"):
                p["nome"] = titulo[:80]
        except requests.RequestException as e:
            print(f"[{p['id']}] erro ao acessar: {e}")
            continue
        if novo is None:
            print(f"[{p['id']}] {p['nome']}: preço não encontrado")
            continue

        antigo = p.get("preco")
        print(f"[{p['id']}] {p['nome']}: {brl(novo)}")
        if antigo is not None and abs(novo - antigo) >= 0.01:
            diff = novo - antigo
            pct = diff / antigo * 100
            seta = "📉 Baixou" if diff < 0 else "📈 Subiu"
            extra = ""
            if p.get("menor_preco") and novo < p["menor_preco"]:
                extra = "\n🏆 Menor preço já registrado!"
            notificar(
                f"{seta}: {p['nome'][:60]}",
                f"De {brl(antigo)} para {brl(novo)} ({pct:+.1f}%){extra}",
                p["url"],
            )
        if antigo is None or abs(novo - antigo) >= 0.01:
            p.setdefault("historico", []).append({"data": agora(), "preco": novo})
        p["preco"] = novo
        p["menor_preco"] = min(filter(None, [p.get("menor_preco"), novo]))
    salvar(produtos)


def cmd_check(_args) -> None:
    checar_todos()


def cmd_run(args) -> None:
    print(f"Monitorando a cada {args.minutos} min. Ctrl+C para parar.")
    while True:
        print(f"\n— {agora()} —")
        checar_todos()
        time.sleep(args.minutos * 60)


def cmd_test_notify(_args) -> None:
    notificar("Teste do Monitor de Preços", "Se você recebeu isso, está funcionando! ✅")


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor de preços")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="adicionar um link")
    a.add_argument("url")
    a.add_argument("--nome")
    a.add_argument("--seletor", help="seletor CSS do preço (opcional)")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list", help="listar produtos").set_defaults(func=cmd_list)

    r = sub.add_parser("remove", help="remover um produto")
    r.add_argument("id", type=int)
    r.set_defaults(func=cmd_remove)

    sub.add_parser("check", help="checar preços uma vez").set_defaults(func=cmd_check)

    rn = sub.add_parser("run", help="checar continuamente")
    rn.add_argument("--minutos", type=float, default=60)
    rn.set_defaults(func=cmd_run)

    sub.add_parser("test-notify", help="testar notificações").set_defaults(func=cmd_test_notify)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
