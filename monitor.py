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

# Configurações locais (ex.: NTFY_TOPICO=...) num arquivo config.env ao lado do monitor.py
for _cfg in ("config.env", ".env", "config.env.txt"):
    _caminho = Path(__file__).with_name(_cfg)
    if _caminho.exists():
        for _linha in _caminho.read_text(encoding="utf-8-sig").splitlines():
            if "=" in _linha and not _linha.lstrip().startswith("#"):
                _k, _v = _linha.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

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
        return
    for p in produtos:
        try:
            novo, titulo = buscar(p["url"], p.get("seletor"))
            if not p.get("nome"):
                p["nome"] = titulo[:80]
        except Exception as e:
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


# --------------------------------------------------------------------------- #
# Monitoramento por BUSCA (todas as placas de um modelo numa loja)
# --------------------------------------------------------------------------- #
ARQUIVO_BUSCAS = Path(os.environ.get("MONITOR_BUSCAS", Path(__file__).with_name("buscas.json")))
LIMITE_AVISOS_SEPARADOS = 6   # acima disso, manda um resumo único


def _bool(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "sim")


def _num(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def buscar_kabum(termo: str, filtro: str) -> dict[str, dict]:
    """Retorna {id: item} com todas as placas da busca na Kabum cujo nome casa com o filtro."""
    regex = re.compile(filtro, re.I)
    slug = re.sub(r"\s+", "-", termo.strip().lower())
    itens: dict[str, dict] = {}
    for pagina in range(1, 6):
        url = f"https://www.kabum.com.br/busca/{slug}?page_number={pagina}&page_size=100"
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        achados: list[dict] = []
        for tag in soup.find_all("script"):
            if tag.get("id") == "__NEXT_DATA__" or tag.get("type") in ("application/json",):
                try:
                    _walk(json.loads(tag.string or ""), achados)
                except ValueError:
                    pass
        produtos = [o for o in achados if o.get("code") and o.get("name")]
        novos = 0
        for o in produtos:
            nome = str(o["name"])
            if not regex.search(nome):
                continue
            if "placa de v" not in (str(o.get("category", "")) + nome).lower():
                continue  # ignora PCs montados, notebooks etc.
            chave = f"kabum:{o['code']}"
            if chave in itens:
                continue
            oferta = o.get("offer") if isinstance(o.get("offer"), dict) else None
            avista = _num((oferta or {}).get("priceWithDiscount")) or _num(o.get("priceWithDiscount")) \
                or _num(o.get("price"))
            parcelado = _num((oferta or {}).get("price")) or _num(o.get("price"))
            disponivel = _bool(o.get("available"))
            vendedor = str(o.get("sellerName") or "KaBuM!")
            itens[chave] = {
                "loja": "Kabum" + ("" if vendedor in ("KaBuM!", "") else f" (vendido por {vendedor})"),
                "nome": nome[:110],
                "url": f"https://www.kabum.com.br/produto/{o['code']}/{o.get('friendlyName', '')}",
                "preco": avista if disponivel else None,
                "parcelado": parcelado if disponivel else None,
                "disponivel": disponivel,
            }
            novos += 1
        if len({o["code"] for o in produtos}) < 100 or novos == 0 and pagina > 1:
            break
    return itens


def _preco_card_ml(card) -> float | None:
    atual = card.select_one(".poly-price__current .andes-money-amount") or \
        next((m for m in card.select(".andes-money-amount")
              if "previous" not in " ".join(m.get("class", []))), None)
    if not atual:
        return None
    if atual.get("aria-label"):
        m = re.search(r"([\d.]+)\s*reais(?:\s*com\s*(\d+)\s*centavos)?", atual["aria-label"])
        if m:
            return float(m.group(1).replace(".", "") + "." + (m.group(2) or "0"))
    frac = atual.select_one(".andes-money-amount__fraction")
    cents = atual.select_one(".andes-money-amount__cents")
    if frac:
        return parse_preco(frac.get_text(strip=True).replace(".", "") + "," + (cents.get_text(strip=True) if cents else "00"))
    return parse_preco(atual.get_text(" ", strip=True))


def buscar_ml_loja(url: str, filtro: str, nome_loja: str) -> dict[str, dict]:
    """Lê a vitrine de uma loja no Mercado Livre e pega as placas que casam com o filtro."""
    regex = re.compile(filtro, re.I)
    resp = requests.get(url, headers=HEADERS, timeout=40)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    itens: dict[str, dict] = {}
    for card in soup.select(".poly-card"):
        titulo_el = card.select_one(".poly-component__title") or card.select_one("a[href]")
        link_el = card.select_one("a[href]")
        if not titulo_el or not link_el:
            continue
        nome = titulo_el.get_text(" ", strip=True)
        if not regex.search(nome) or "placa" not in nome.lower():
            continue
        link = link_el["href"].split("#")[0]
        m = re.search(r"(MLBU?-?\d+)", link)
        chave = f"ml:{m.group(1) if m else link.split('?')[0]}"
        itens[chave] = {
            "loja": f"{nome_loja} (Mercado Livre)",
            "nome": nome[:110],
            "url": link.split("?")[0] if m else link,
            "preco": _preco_card_ml(card),
            "parcelado": None,
            "disponivel": True,
        }
    return itens


def _cards_patoloco(soup: BeautifulSoup, filtro: re.Pattern) -> dict[str, dict]:
    itens: dict[str, dict] = {}
    for card in soup.select("div.product-info"):
        link_el = next((a for a in card.select("a[href]")
                        if "patoloco.com.br/" in a["href"] and "/produtos/" not in a["href"]), None)
        if not link_el:
            continue
        url = link_el["href"].split("?")[0].split("#")[0]
        nome = (link_el.get("title") or link_el.get_text(" ", strip=True)
                or card.get_text(" | ", strip=True).split(" | ")[0]).strip()
        if not filtro.search(nome) or "placa" not in nome.lower():
            continue
        texto = card.get_text(" ", strip=True)
        pix = re.search(r"R\$\s*([\d.]+,\d{2})\s*à vista", texto)
        parc = re.search(r"(\d+)x\s*de\s*R\$\s*([\d.]+,\d{2})", texto)
        esgotado = bool(re.search(r"esgotad|indispon|avise-me", texto, re.I))
        preco = parse_preco(pix.group(1)) if pix and not esgotado else None
        itens[f"patoloco:{url.rstrip('/').rsplit('/', 1)[-1]}"] = {
            "loja": "Pato Loco",
            "nome": nome[:110],
            "url": url,
            "preco": preco,
            "parcelado": (int(parc.group(1)) * parse_preco(parc.group(2))) if parc and preco else None,
            "disponivel": preco is not None,
        }
    return itens


def buscar_patoloco(url: str, filtro: str) -> dict[str, dict]:
    """Lê todas as páginas da categoria da Pato Loco (/categoria, /categoria/2, ...)."""
    regex = re.compile(filtro, re.I)
    base = url.split("?")[0].rstrip("/")
    itens: dict[str, dict] = {}
    urls_vistas: set[str] = set()
    for pagina in range(1, 21):
        resp = requests.get(base if pagina == 1 else f"{base}/{pagina}", headers=HEADERS, timeout=40)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.product-info")
        links = {a["href"].split("?")[0] for c in cards for a in c.select("a[href]")
                 if "patoloco.com.br/" in a["href"] and "/produtos/" not in a["href"]}
        if not cards or links <= urls_vistas:
            break  # página vazia ou repetida = acabou
        urls_vistas |= links
        itens.update(_cards_patoloco(soup, regex))
    print(f"   Pato Loco: {pagina - 1 if not cards or links <= urls_vistas else pagina} páginas, {len(urls_vistas)} produtos lidos")
    return itens


def buscar_vtex(dominio: str, termo: str, filtro: str, nome_loja: str) -> dict[str, dict]:
    """Usa a busca pública de lojas na plataforma VTEX (Americanas e outras)."""
    regex = re.compile(filtro, re.I)
    itens: dict[str, dict] = {}
    for inicio in range(0, 250, 50):
        url = (f"https://{dominio}/api/catalog_system/pub/products/search"
               f"?ft={requests.utils.quote(termo)}&_from={inicio}&_to={inicio + 49}")
        resp = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=40)
        if resp.status_code in (206, 200):
            lista = resp.json()
        else:
            resp.raise_for_status()
            lista = []
        for p in lista:
            nome = str(p.get("productName") or "")
            if not regex.search(nome) or "placa" not in nome.lower():
                continue
            ofertas = []
            for sku in p.get("items", []):
                for vend in sku.get("sellers", []):
                    co = vend.get("commertialOffer") or {}
                    if (co.get("AvailableQuantity") or 0) > 0 and _num(co.get("Price")):
                        ofertas.append((float(co["Price"]), vend.get("sellerName") or nome_loja))
            preco, vendedor = min(ofertas) if ofertas else (None, "")
            link = p.get("link") or f"https://{dominio}/{p.get('linkText', '')}/p"
            loja = nome_loja + (f" (vendido por {vendedor})" if vendedor and vendedor.lower() not in nome_loja.lower() else "")
            itens[f"vtex:{dominio}:{p.get('productId')}"] = {
                "loja": loja, "nome": nome[:110], "url": link.split("?")[0],
                "preco": preco, "parcelado": None, "disponivel": preco is not None,
            }
        if len(lista) < 50:
            break
    return itens


_LINK_PRODUTO = re.compile(r"^(https?://[^/]+)?/produto/")
_SEM_ESTOQUE = re.compile(r"esgotad|indispon|avise-me|todos vendidos|sem estoque", re.I)


def _links_produto(no) -> set[str]:
    return {x["href"].split("?")[0] for x in no.select("a[href]") if _LINK_PRODUTO.match(x["href"])}


def _card_do_link(link):
    """Sobe a partir do link até o menor bloco que contém o preço no PIX de um único produto."""
    no = link
    for i in range(11):
        if i > 0:
            no = no.parent
        if no is None:
            return None
        if len(_links_produto(no)) > 1:
            return None
        texto = no.get_text(" ", strip=True)
        if re.search(r"no\s*pix", texto, re.I) or _SEM_ESTOQUE.search(texto):
            return no
    return None


def buscar_cards_pix(url_busca: str, termo: str, filtro: str, nome_loja: str, prefixo: str,
                     param_pagina: str | None = "pagina") -> dict[str, dict]:
    """Lê páginas de busca cujos cards mostram 'R$ X ... no Pix' (TNT Info, Terabyte...)."""
    regex = re.compile(filtro, re.I)
    base = re.match(r"https?://[^/]+", url_busca).group(0)
    itens: dict[str, dict] = {}
    vistos: set[str] = set()
    for pagina in range(1, 8):
        url = url_busca.format(termo=requests.utils.quote(termo).replace("%20", "+"))
        if pagina > 1:
            if not param_pagina:
                break
            url += f"&{param_pagina}={pagina}"
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        links = {}
        for a in soup.select("a[href]"):
            if _LINK_PRODUTO.match(a["href"]):
                links.setdefault(a["href"].split("?")[0], a)
        novos = set(links) - vistos
        if not novos:
            break
        vistos |= novos
        for href in novos:
            a = links[href]
            card = _card_do_link(a)
            if card is None:
                continue
            img = card.select_one("img[alt]")
            tit = card.select_one("h1, h2, h3, h4, [class*=name], [class*=title], [class*=nome]")
            candidatos = [a.get("title"), tit.get_text(" ", strip=True) if tit else None,
                          img.get("alt") if img else None]
            candidatos += card.get_text(" | ", strip=True).split(" | ")
            nome = next((c.strip() for c in candidatos
                         if c and len(c.strip()) >= 20 and "R$" not in c and "placa" in c.lower()), "")
            nome = nome or re.split(r"\s*R\$", a.get_text(" ", strip=True))[0]
            if not regex.search(nome) or "placa" not in nome.lower():
                continue
            texto = card.get_text(" ", strip=True)
            antes_pix = re.split(r"no\s*pix", texto, flags=re.I)[0]
            valores = re.findall(r"R\$\s*([\d.]+,\d{2})", antes_pix)
            tem_pix = re.search(r"no\s*pix", texto, re.I)
            preco = parse_preco(valores[-1]) if valores and tem_pix and not _SEM_ESTOQUE.search(texto) else None
            parc = re.search(r"(\d+)\s*x\s*de\s*R\$\s*([\d.]+,\d{2})\s*sem juros", texto)
            link = href if href.startswith("http") else base + href
            m = re.search(r"/produto/(\d+)/", link)
            ident = m.group(1) if m else link.rstrip("/").rsplit("-", 1)[-1]
            itens[f"{prefixo}:{ident}"] = {
                "loja": nome_loja, "nome": nome[:110], "url": link, "preco": preco,
                "parcelado": int(parc.group(1)) * parse_preco(parc.group(2)) if parc and preco else None,
                "disponivel": preco is not None,
            }
    return itens


def buscar_tnt(termo: str, filtro: str) -> dict[str, dict]:
    return buscar_cards_pix("https://www.tntinfo.com.br/busca?busca={termo}", termo, filtro, "TNT Info", "tnt")


def buscar_terabyte(termo: str, filtro: str) -> dict[str, dict]:
    return buscar_cards_pix("https://www.terabyteshop.com.br/busca?str={termo}", termo, filtro,
                            "Terabyte", "terabyte", param_pagina=None)


_EXCLUIR_PROMOBIT = re.compile(r"notebook|laptop|pc gamer|computador|desktop|\bkit\b|combo", re.I)


def buscar_promobit(urls: list[str], filtro: str) -> dict[str, dict]:
    """Lê ofertas postadas no Promobit (dados da página) e pega as de placas que casam com o filtro."""
    from collections import Counter
    regex = re.compile(filtro, re.I)
    itens: dict[str, dict] = {}
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=40)
        except Exception as e:
            print(f"   Promobit {url}: erro {e}")
            continue
        if resp.status_code != 200:
            print(f"   Promobit {url}: HTTP {resp.status_code}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        achados: list[dict] = []
        for tag in soup.find_all("script"):
            if tag.get("id") == "__NEXT_DATA__" or tag.get("type") == "application/json":
                try:
                    _walk(json.loads(tag.string or ""), achados)
                except ValueError:
                    pass
        ofertas = {str(o["offerId"]): o for o in achados if o.get("offerId") and o.get("offerTitle")}
        status = Counter(str(o.get("offerStatusName")) for o in ofertas.values())
        casam = 0
        for oid, o in ofertas.items():
            titulo = str(o["offerTitle"])
            if not regex.search(titulo) or _EXCLUIR_PROMOBIT.search(titulo):
                continue
            casam += 1
            st = str(o.get("offerStatusName", "")).upper()
            ativa = not re.search(r"FINISH|EXPIR|CLOSE|ENDED|ENCERR|REJECT|DELET|INACTIVE|SOLD", st)
            loja = str(o.get("storeName") or "loja")
            cupom = o.get("offerCoupon")
            itens[f"promobit:{oid}"] = {
                "loja": f"Promobit → {loja}" + (f" (cupom {cupom})" if cupom and str(cupom) != "None" else ""),
                "nome": titulo[:110],
                "url": f"https://www.promobit.com.br/oferta/{o.get('offerSlug', oid)}/",
                "preco": _num(o.get("offerPrice")) if ativa else None,
                "parcelado": None,
                "disponivel": ativa,
            }
        cats = Counter(f"{o.get('categorySlug')}/{o.get('subcategorySlug')}" for o in ofertas.values())
        print(f"   Promobit {url}: {len(ofertas)} ofertas {dict(status)}, {casam} de 5070 Ti/5080")
        datas = sorted(str(o.get("offerPublished", ""))[:16].replace("T", " ") for o in ofertas.values() if o.get("offerPublished"))
        print(f"      categorias: {dict(cats.most_common(8))}")
        if datas:
            print(f"      postadas entre {datas[0]} e {datas[-1]}")
    return itens


def buscar_aliexpress(termo: str, filtro: str, paginas: int = 2) -> dict[str, dict]:
    """Lê a busca do AliExpress Brasil (pt.aliexpress.com/w/wholesale-<termo>.html)."""
    regex = re.compile(filtro, re.I)
    slug = re.sub(r"\s+", "-", termo.strip().lower())
    itens: dict[str, dict] = {}
    for pagina in range(1, paginas + 1):
        url = f"https://pt.aliexpress.com/w/wholesale-{slug}.html" + (f"?page={pagina}" if pagina > 1 else "")
        resp = requests.get(url, headers=HEADERS, timeout=40)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        if re.search(r"captcha|punish|slide to verify", resp.text[:20000], re.I) and not soup.select('[class*="card-out-wrapper"]'):
            print(f"   AliExpress pág. {pagina}: pediu verificação (captcha) — tento de novo na próxima")
            break
        antes = len(itens)
        for card in soup.select('[class*="card-out-wrapper"]'):
            a = card.select_one('a[href*="/item/"]')
            m = re.search(r"/item/(\d+)\.html", a["href"]) if a else None
            if not m or f"aliexpress:{m.group(1)}" in itens:
                continue
            tit = card.select_one("h1, h2, h3, [class*=title], [title]")
            pedacos = card.get_text(" | ", strip=True).split(" | ")
            candidatos = [tit.get("title") if tit else None, tit.get_text(" ", strip=True) if tit else None] + pedacos
            nome = next((c.strip() for c in candidatos if c and len(c.strip()) >= 15 and "R$" not in c), "")
            if not nome or not regex.search(nome):
                continue
            valores = re.findall(r"R\$\s*([\d.]+,\d{2})", card.get_text(" ", strip=True))
            preco = parse_preco(valores[0]) if valores else None
            itens[f"aliexpress:{m.group(1)}"] = {
                "loja": "AliExpress", "nome": nome[:110],
                "url": f"https://pt.aliexpress.com/item/{m.group(1)}.html",
                "preco": preco, "parcelado": None, "disponivel": preco is not None,
            }
        if len(itens) == antes:
            break
    return itens


def carregar_buscas() -> dict:
    if ARQUIVO_BUSCAS.exists():
        return json.loads(ARQUIVO_BUSCAS.read_text(encoding="utf-8"))
    return {"buscas": [], "itens": {}}


def salvar_buscas(dados: dict) -> None:
    ARQUIVO_BUSCAS.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


def checar_buscas() -> None:
    dados = carregar_buscas()
    if not dados.get("buscas"):
        return
    estado = dados.setdefault("itens", {})
    eventos: list[tuple[str, str, str]] = []   # (título, mensagem, link)

    for b in dados["buscas"]:
        rotulo = b.get("rotulo") or b.get("termo") or b.get("url")
        try:
            if b["tipo"] == "kabum":
                encontrados = buscar_kabum(b["termo"], b["filtro"])
            elif b["tipo"] == "aliexpress":
                encontrados = buscar_aliexpress(b["termo"], b["filtro"])
            elif b["tipo"] == "promobit":
                encontrados = buscar_promobit(b["urls"], b["filtro"])
            elif b["tipo"] == "terabyte":
                encontrados = buscar_terabyte(b["termo"], b["filtro"])
            elif b["tipo"] == "tnt":
                encontrados = buscar_tnt(b["termo"], b["filtro"])
            elif b["tipo"] == "vtex":
                encontrados = buscar_vtex(b["dominio"], b["termo"], b["filtro"], b.get("nome_loja", b["dominio"]))
            elif b["tipo"] == "patoloco":
                encontrados = buscar_patoloco(b["url"], b["filtro"])
            elif b["tipo"] == "ml_loja":
                encontrados = {}
                for u in b.get("urls") or [b["url"]]:
                    encontrados.update(buscar_ml_loja(u, b["filtro"], b.get("nome_loja", "Loja")))
            else:
                print(f"tipo de busca desconhecido: {b['tipo']}")
                continue
        except Exception as e:
            print(f"[busca {rotulo}] erro — {e}")
            continue

        primeira_vez = not b.get("inicializada")
        com_preco = sum(1 for i in encontrados.values() if i["preco"])
        print(f"[busca {rotulo}] {len(encontrados)} anúncios ({com_preco} com estoque/preço)")

        for chave, item in encontrados.items():
            ant = estado.get(chave)
            novo = item["preco"]
            if ant is None:
                item["menor_preco"] = novo
                item["historico"] = [{"data": agora(), "preco": novo}] if novo else []
                item["visto_em"] = agora()
                estado[chave] = item
                if primeira_vez:
                    continue
                if chave.startswith("promobit:"):
                    if novo:  # oferta postada no Promobit e ainda ativa
                        loja = item["loja"].replace("Promobit → ", "")
                        eventos.append((f"🔥 Promobit: {item['nome'][:60]}", f"{brl(novo)} na {loja}", item["url"]))
                    continue
                eventos.append((f"🆕 Novo anúncio: {item['nome'][:60]}",
                                f"{item['loja']}\n{brl(novo) if novo else 'sem estoque'}", item["url"]))
                continue

            velho = ant.get("preco")
            nome = item["nome"][:60]
            if novo and velho and abs(novo - velho) >= 0.01:
                pct = (novo - velho) / velho * 100
                seta = "📉 Baixou" if novo < velho else "📈 Subiu"
                extra = "\n🏆 Menor preço já registrado!" if ant.get("menor_preco") and novo < ant["menor_preco"] else ""
                eventos.append((f"{seta}: {nome}",
                                f"{item['loja']}\nDe {brl(velho)} para {brl(novo)} ({pct:+.1f}%){extra}",
                                item["url"]))
            elif novo and not velho:
                eventos.append((f"✅ Voltou ao estoque: {nome}", f"{item['loja']}\n{brl(novo)}", item["url"]))
            elif velho and not novo and ant.get("disponivel") and not chave.startswith("promobit:"):
                eventos.append((f"⛔ Esgotou: {nome}", f"{item['loja']}\nÚltimo preço: {brl(velho)}", item["url"]))

            if novo != velho:
                hist = ant.setdefault("historico", [])
                hist.append({"data": agora(), "preco": novo})
                del hist[:-60]
            ant.update({k: item[k] for k in ("loja", "nome", "url", "preco", "parcelado", "disponivel")})
            if novo:
                ant["menor_preco"] = min(filter(None, [ant.get("menor_preco"), novo]))
            ant["visto_em"] = agora()

        b["inicializada"] = True

    if len(eventos) > LIMITE_AVISOS_SEPARADOS:
        linhas = [f"{t}\n   {m.splitlines()[-1]}" for t, m, _ in eventos]
        texto = "\n".join(linhas)
        if len(texto) > 3500:
            texto = texto[:3500] + "\n…"
        notificar(f"🔔 {len(eventos)} mudanças de preço/estoque", texto)
    else:
        for t, m, link in eventos:
            notificar(t, m, link)
    # Se outra cópia do app (ex.: o 'atualizar') incluiu buscas novas enquanto esta conferência rodava,
    # mantém essas buscas em vez de sobrescrever o arquivo sem elas.
    try:
        no_disco = carregar_buscas().get("buscas", [])
        conhecidas = {b.get("rotulo") for b in dados["buscas"]}
        dados["buscas"].extend(b for b in no_disco if b.get("rotulo") not in conhecidas)
    except Exception:
        pass
    salvar_buscas(dados)


def cmd_buscas(_args) -> None:
    dados = carregar_buscas()
    for b in dados.get("buscas", []):
        print(f"• {b.get('rotulo')}  ({b['tipo']})")
    itens = sorted(dados.get("itens", {}).values(), key=lambda i: (i["preco"] is None, i["preco"] or 0))
    for i in itens:
        print(f"  {brl(i['preco']):>14}  {i['loja'][:28]:<28}  {i['nome'][:70]}")


def cmd_check(_args) -> None:
    checar_todos()
    checar_buscas()


def _abrir_log() -> None:
    """Quando roda escondido (pythonw), grava tudo em monitor.log."""
    log = Path(__file__).with_name("monitor.log")
    if log.exists() and log.stat().st_size > 5_000_000:
        log.replace(log.with_suffix(".log.old"))
    f = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def cmd_run(args) -> None:
    if sys.stdout is None or args.log:
        _abrir_log()
    print(f"Monitorando a cada {args.minutos} min. Ctrl+C para parar.")
    while True:
        inicio = time.time()
        print(f"\n— {agora()} —")
        try:
            checar_todos()
            checar_buscas()
        except Exception as e:  # nunca para por causa de um erro pontual
            print(f"erro inesperado: {e!r} — tento de novo na próxima")
        time.sleep(max(10, args.minutos * 60 - (time.time() - inicio)))


def _walk(obj, achados, profundidade=0):
    """Procura, em JSON aninhado, objetos que pareçam produtos (nome + preço)."""
    if profundidade > 40:
        return
    if isinstance(obj, str) and obj[:1] in "[{" and len(obj) > 50:
        try:
            _walk(json.loads(obj), achados, profundidade + 1)
        except ValueError:
            pass
    elif isinstance(obj, list):
        for o in obj:
            _walk(o, achados, profundidade + 1)
    elif isinstance(obj, dict):
        chaves = {k.lower() for k in obj}
        if any("name" in k or "nome" in k or "title" in k for k in chaves) and any("price" in k or "preco" in k for k in chaves):
            achados.append(obj)
        for v in obj.values():
            _walk(v, achados, profundidade + 1)


def cmd_debug(args) -> None:
    for url in args.urls:
        if url.startswith("tnt:"):  # testa a busca da TNT: tnt:rtx+5080
            termo = url[4:].replace("+", " ")
            res = buscar_tnt(termo, r"(?<!\d)5070\s*-?\s*ti\b|(?<!\d)5080(?!\d)")
            print(f"busca TNT '{termo}': {len(res)} placas")
            for k, v in res.items():
                print(f"   {brl(v['preco']):>14}  {v['nome'][:80]}  [{k}]")
            continue
        print("=" * 70)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
        except Exception as e:
            print(f"{url}\n  ERRO: {e}")
            continue
        html = resp.text
        if "json" in resp.headers.get("content-type", ""):
            try:
                dados = resp.json()
                prim = dados[0] if isinstance(dados, list) and dados else dados
                print(f"{url}\n  HTTP {resp.status_code} · JSON · {len(dados) if isinstance(dados, list) else 'obj'} itens"
                      f" · 1º: {str(prim.get('productName') if isinstance(prim, dict) else prim)[:100]}")
            except ValueError:
                print(f"{url}\n  HTTP {resp.status_code} · JSON inválido")
            continue
        soup = BeautifulSoup(html, "html.parser")
        print(f"{url}\n  HTTP {resp.status_code} · {len(html)} bytes · título: {_titulo(soup)[:90]}")
        print(f"  preço (página de produto): {brl(extrair_preco(html)[0])}")
        print(f"  links /produto/: {len(soup.select('a[href*=\"/produto/\"]'))}"
              f" · JSON-LD: {len(soup.find_all('script', type='application/ld+json'))}")
        achados = []
        for tag in soup.find_all("script"):
            txt = (tag.string or "").strip()
            if tag.get("type") in ("application/json", "application/ld+json") or tag.get("id") == "__NEXT_DATA__":
                try:
                    _walk(json.loads(txt), achados)
                except ValueError:
                    pass
        print(f"  objetos tipo produto em JSON: {len(achados)}")
        produtos_json = [o for o in achados if "code" in o] or achados
        print(f"  com 'code': {len([o for o in achados if 'code' in o])}")
        for o in produtos_json[:2]:
            print("    chaves:", sorted(o.keys()))
            interessantes = {k: str(v)[:160] for k, v in o.items()
                             if re.search(r"price|preco|offer|avail|stock|url|link|name|code|seller", k, re.I)}
            print("   ", json.dumps(interessantes, ensure_ascii=False)[:1200])
        hrefs = sorted({x["href"].split("?")[0] for x in soup.select("a[href]")
                        if re.search(r"lista|_Loja|/loja/|busca|search|placa|video|vga", x["href"], re.I) and "/produtos" in x["href"] or re.search(r"lista|_Loja|/loja/", x["href"])})
        if hrefs:
            print(f"  links de listagem ({len(hrefs)}):", " ".join(hrefs[:15]))
        promo = sorted({x["href"].split("?")[0] for x in soup.select("a[href]")
                        if x["href"].startswith("/promocoes/") and "/loja/" not in x["href"]})
        if promo:
            print(f"  links /promocoes/ ({len(promo)}):", " ".join(promo[:60]))
        placas = [c.get_text(" ", strip=True)[:90] for c in soup.select(".poly-card")
                  if re.search(r"5070|5080|placa", c.get_text(" ", strip=True), re.I)]
        print(f"  cards com placa/5070/5080: {len(placas)}", placas[:4])
        for f in soup.select("form")[:4]:
            print(f"  form: action={f.get('action')} method={f.get('method')} inputs={[i.get('name') for i in f.select('input')][:6]}")
        prods = soup.select('a[href*="/produto/"]')
        for x in prods[:2]:
            pai = x.find_parent(["li", "article", "div"])
            print(f"  produto: {x['href'][:110]}")
            if pai:
                print(f"     pai {pai.name}.{'.'.join(pai.get('class', []))[:50]}: {pai.get_text(' | ', strip=True)[:220]}")
        precos_el = soup.select("[class*=price]")
        vistos = 0
        for el in precos_el:
            anc = el
            for _ in range(8):
                anc = anc.parent
                if anc is None:
                    break
                link = anc.select_one("a[href]")
                if link:
                    print(f"  card: {anc.name}.{'.'.join(anc.get('class', []))[:60]} → {link['href'][:110]}")
                    print(f"        {anc.get_text(' | ', strip=True)[:260]}")
                    vistos += 1
                    break
            if vistos >= 3:
                break
        from collections import Counter
        segs = Counter(x["href"].split("/")[3] if x["href"].startswith("http") and x["href"].count("/") > 3
                       else x["href"].split("/")[1] if x["href"].startswith("/") and x["href"].count("/") >= 1 else "?"
                       for x in soup.select("a[href]"))
        print("  padrões de link:", segs.most_common(12))
        pag = sorted({x["href"] for x in soup.select("a[href]") if re.search(r"pagin|page|[?&]p=|offset|limit", x["href"], re.I)})
        print(f"  links de paginação ({len(pag)}):", " ".join(pag[:12]))
        for el in soup.select("[class*=pagin], [class*=Pagin], [data-page], [class*=load-more], [class*=ver-mais]")[:4]:
            print("  paginação el:", el.name, el.get("class"), {k: v for k, v in el.attrs.items() if k.startswith("data")}, el.get_text(" ", strip=True)[:120])
        for sc in soup.find_all("script"):
            t = sc.string or ""
            m = re.search(r".{0,80}(pagina|totalPages|total_pages|ajax|loadMore|/api/).{0,120}", t, re.I)
            if m:
                print("  script:", m.group(0).replace("\n", " ")[:220])
        for sel in (".poly-card", ".ui-search-layout__item", ".andes-card"):
            cards = soup.select(sel)
            if cards:
                print(f"  cards {sel}: {len(cards)}")
                for c in cards[:3]:
                    a = c.select_one("a[href]")
                    print("    -", c.get_text(" | ", strip=True)[:200], "→", (a["href"][:120] if a else ""))
                break
        if not achados:
            for sel in ("[class*=productCard]", "[class*=product-item]", "[class*=pbox]", "[class*=price]"):
                el = soup.select(sel)
                if el:
                    print(f"  {sel}: {len(el)} → {el[0].get_text(' ', strip=True)[:150]}")


REPO_RAW = "https://raw.githubusercontent.com/DestroyDinamite/DinamitePrice/main/"


def _raw_mais_novo() -> str:
    """Endereço dos arquivos na versão (commit) mais recente, para não pegar cópia antiga em cache."""
    try:
        r = requests.get("https://api.github.com/repos/DestroyDinamite/DinamitePrice/commits/main",
                         headers={"Accept": "application/vnd.github.sha"}, timeout=20)
        if r.status_code == 200 and re.fullmatch(r"[0-9a-f]{40}", r.text.strip()):
            return f"https://raw.githubusercontent.com/DestroyDinamite/DinamitePrice/{r.text.strip()}/"
    except Exception:
        pass
    return REPO_RAW


def cmd_atualizar(_args) -> None:
    """Baixa a versão mais nova do monitor.py e inclui buscas novas, sem mexer no histórico."""
    raw = _raw_mais_novo()
    print(f"Baixando de {raw}")
    codigo = requests.get(raw + "monitor.py", timeout=30)
    codigo.raise_for_status()
    if "def main" not in codigo.text:
        sys.exit("Download do monitor.py veio incompleto; nada foi alterado.")
    atual = Path(__file__)
    if atual.read_text(encoding="utf-8") != codigo.text:
        atual.with_suffix(".py.bak").write_text(atual.read_text(encoding="utf-8"), encoding="utf-8")
        atual.write_text(codigo.text, encoding="utf-8")
        print("✔ monitor.py atualizado (cópia anterior em monitor.py.bak)")
    else:
        print("✔ monitor.py já estava na versão mais nova")

    remotas = requests.get(raw + "buscas.json", timeout=30).json().get("buscas", [])
    dados = carregar_buscas()
    rotulos = {b.get("rotulo") for b in dados.get("buscas", [])}
    novas = [dict(b, inicializada=False) for b in remotas if b.get("rotulo") not in rotulos]
    alteradas = 0
    por_rotulo = {b.get("rotulo"): b for b in remotas}
    for b in dados.get("buscas", []):
        r = por_rotulo.get(b.get("rotulo"))
        if r:
            novo = {k: v for k, v in r.items() if k != "inicializada"}
            if any(b.get(k) != v for k, v in novo.items()):
                b.update(novo)
                alteradas += 1
                print(f"✔ busca atualizada: {b['rotulo']}")
    if novas or alteradas:
        dados.setdefault("buscas", []).extend(novas)
        salvar_buscas(dados)
    for b in novas:
        print(f"✔ busca nova: {b['rotulo']}")
    if not novas and not alteradas:
        print("✔ nenhuma busca nova ou alterada")
    print("Agora reinicie o monitor para ele usar a versão nova:")
    print("   Stop-ScheduledTask DinamitePrice; Start-ScheduledTask DinamitePrice")


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
    sub.add_parser("buscas", help="listar placas encontradas nas buscas").set_defaults(func=cmd_buscas)

    rn = sub.add_parser("run", help="checar continuamente")
    rn.add_argument("--minutos", type=float, default=5)
    rn.add_argument("--log", action="store_true", help="gravar a saída em monitor.log")
    rn.set_defaults(func=cmd_run)

    sub.add_parser("test-notify", help="testar notificações").set_defaults(func=cmd_test_notify)
    sub.add_parser("atualizar", help="baixar a versão nova e buscas novas").set_defaults(func=cmd_atualizar)

    d = sub.add_parser("debug", help="mostrar o que o app enxerga nas páginas")
    d.add_argument("urls", nargs="+")
    d.set_defaults(func=cmd_debug)

    # No Windows, ao salvar a saída em arquivo, caracteres como → ou emojis não podem derrubar o app
    for fluxo in (sys.stdout, sys.stderr):
        if fluxo is not None and hasattr(fluxo, "reconfigure"):
            try:
                fluxo.reconfigure(errors="replace")
            except Exception:
                pass

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
