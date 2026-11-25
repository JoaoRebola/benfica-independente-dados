# -*- coding: utf-8 -*-
"""Classificacoes oficiais da epoca 2026/27 em que o SL Benfica joga (fontes extra).

Fontes:
  - fpb.pt (basquetebol): tabela server-rendered em /classificacao/<id>
  - zerozero.pt (futebol feminino, Liga BPI): tabela /edicao/<slug>/<id>/classificacao

Apenas biblioteca standard do Python. Pausa de 2.5 s entre pedidos.
Nada e inventado: colunas em falta nao sao preenchidas, e tabelas que falhem
a validacao sao descartadas.
"""
import html as _html
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
PAUSA = 2.5
BENFICA = "SL Benfica"

# (url, fonte, modalidade s, chave k, nome da competicao c)
FONTES = [
    ("https://www.fpb.pt/classificacao/11440", "fpb",
     "Basquetebol", "", "Liga Betclic Masculina"),
    ("https://www.fpb.pt/classificacao/11441", "fpb",
     "Basquetebol", "", "Proliga"),
    ("https://www.fpb.pt/classificacao/11445", "fpb",
     "Basquetebol Feminino", "", "Liga Betclic Feminina"),
]

_ultimo_pedido = [0.0]


def _get(url):
    """Pedido HTTP com pausa entre pedidos. Levanta RuntimeError em erro HTTP."""
    espera = PAUSA - (time.time() - _ultimo_pedido[0])
    if espera > 0:
        time.sleep(espera)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "pt-PT,pt;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            _ultimo_pedido[0] = time.time()
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        _ultimo_pedido[0] = time.time()
        raise RuntimeError("HTTP %s em %s" % (e.code, url))


def _texto(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return _html.unescape(re.sub(r"\s+", " ", s)).strip()


def _fpb_tabela(page):
    """Basquetebol FPB: colunas J V D FC PM PS DIF PTS (sem empates)."""
    linhas = []
    for bloco in page.split('class="team-row')[1:]:
        pos = re.search(r'class="number[^"]*">\s*<h5>\s*(?:<b>)?\s*(\d+)', bloco)
        nome = re.search(r'class="only-desktop">\s*(.*?)\s*</h5>', bloco, re.S)
        if not pos or not nome:
            continue
        nums = re.findall(r'<h5>\s*(?:<b>)?\s*(-?\d+)\s*(?:</b>)?\s*</h5>', bloco[nome.end():])
        if len(nums) < 8:
            continue
        J, V, D, FC, PM, PS, DIF, PTS = [int(x) for x in nums[:8]]
        equipa = _texto(nome.group(1))
        # Basquetebol nao tem empates: E = 0 (coluna nao existe na fonte).
        linhas.append([int(pos.group(1)), equipa, PTS, J, V, 0, D, PM, PS])
    return linhas


def _zerozero_tabela(page):
    """Futebol zerozero: colunas P J V E D GM GS (DG e ignorado)."""
    m = re.search(r'<table[^>]*zz-datatable[^>]*>(.*?)</table>', page, re.S)
    if not m:
        return []
    linhas = []
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(1), re.S):
        cels = [_texto(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
        if len(cels) < 11 or not cels[0].isdigit():
            continue
        try:
            pos = int(cels[0])
            equipa = cels[2]
            P, J, V, E, D, GM, GS = [int(x) for x in cels[3:10]]
        except ValueError:
            continue
        if equipa == "Benfica":
            # Mesmo clube, nome normalizado para o rotulo exigido.
            equipa = BENFICA
        linhas.append([pos, equipa, P, J, V, E, D, GM, GS])
    return linhas


def _valida(linhas, fonte):
    """Devolve (ok, motivo). V+E+D == J em todas as equipas (regra geral)."""
    if not linhas:
        return False, "sem linhas"
    for l in linhas:
        pos, equipa, pts, J, V, E, D, GM, GS = l
        if V + E + D != J:
            return False, "V+E+D != J em %s" % equipa
        if fonte == "fpb" and pts != 2 * V + D:
            return False, "pontos != 2V+D em %s" % equipa
        if fonte == "zerozero" and pts != 3 * V + E:
            return False, "pontos != 3V+E em %s" % equipa
    return True, "ok"


def extra_classificacoes():
    resultado = []
    for url, fonte, s, k, c in FONTES:
        try:
            page = _get(url)
        except Exception as e:  # rede, bloqueio, 403/429
            print("[extra] %s: nao lido (%s)" % (c, e))
            continue
        if fonte == "fpb":
            tab = _fpb_tabela(page)
        else:
            tab = _zerozero_tabela(page)
        if not tab:
            print("[extra] %s: tabela nao encontrada" % c)
            continue
        ok, motivo = _valida(tab, fonte)
        if not ok:
            print("[extra] %s: descartada (%s)" % (c, motivo))
            continue
        if not any(l[1] == BENFICA for l in tab):
            print("[extra] %s: sem %s, ignorada" % (c, BENFICA))
            continue
        resultado.append({
            "s": s,
            "k": k,
            "c": c,
            "t": tab,
            "src": "extra-" + fonte,
        })
    return resultado


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    for comp in extra_classificacoes():
        bn = [l for l in comp["t"] if l[1] == BENFICA][0]
        print("%s | %s | %s | equipas=%d | Benfica=%s" % (
            comp["s"], comp["c"], comp["src"], len(comp["t"]), bn))
