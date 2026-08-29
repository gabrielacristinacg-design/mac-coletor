
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
import streamlit as st
from bs4 import BeautifulSoup, Tag

BASE = "https://www.cartolafcbrasil.com.br"
INDEX_URL = f"{BASE}/ScoutsCartola.aspx"

POSICOES_VALIDAS = {"GOL", "LAT", "ZAG", "MEI", "ATA"}
SCOUT_CODES = {
    "DS","G","A","SG","FS","FF","FD","FT","PS","DE","DP",
    "GC","CV","CA","GS","PP","PC","FC","I","V"
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return clean(s).casefold()

def athlete_id_from_url(url):
    m = re.search(r"/atleta/[^/]+/(\d+)/", url)
    return m.group(1) if m else None

def club_slug_from_url(url):
    m = re.search(r"/atleta/([^/]+)/\d+/", url)
    return m.group(1) if m else None

def round_from_index_text(text):
    m = re.search(r"Filtro de Atletas\s*\(Rodada\s*#\s*(\d+)", text, re.I)
    return int(m.group(1)) if m else None

def discover_players(timeout=25):
    r = requests.get(INDEX_URL, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))
    rodada_detectada = round_from_index_text(page_text)

    found = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/atleta/" not in href:
            continue

        label = clean(a.get_text(" ", strip=True))
        m = re.match(r"(.+?)\s*\((GOL|LAT|ZAG|MEI|ATA|TEC)\)\s*$", label, re.I)
        if not m:
            continue

        nome, posicao = m.groups()
        posicao = posicao.upper()
        if posicao not in POSICOES_VALIDAS:
            continue

        url = urljoin(BASE, href)
        atleta_id = athlete_id_from_url(url)
        if not atleta_id:
            continue

        # O clube oficial será validado na página individual.
        # O slug fica apenas como pista de descoberta/auditoria.
        found[atleta_id] = {
            "atleta_id": atleta_id,
            "nome_indice": clean(nome),
            "posicao_indice": posicao,
            "clube_slug_indice": club_slug_from_url(url),
            "url": url,
        }

    jogadores = sorted(
        found.values(),
        key=lambda x: (x["posicao_indice"], norm(x["nome_indice"]))
    )
    return rodada_detectada, jogadores

def parse_entry(text, contexto):
    text = clean(text)
    m = re.search(
        r"(\d+)\s*x\s*(\d+)\s*\(Rodada\s*#\s*(\d+)\)\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$",
        text,
        re.I,
    )
    if not m:
        return None

    gols_a, gols_b, rodada, pontos, resto = m.groups()
    tokens = resto.split()
    scouts = {}
    i = 0
    while i + 1 < len(tokens):
        if re.fullmatch(r"-?\d+(?:[.,]\d+)?", tokens[i]) and tokens[i + 1].upper() in SCOUT_CODES:
            qtd = float(tokens[i].replace(",", "."))
            if qtd.is_integer():
                qtd = int(qtd)
            scouts[tokens[i + 1].upper()] = qtd
            i += 2
        else:
            i += 1

    return {
        "rodada": int(rodada),
        "pontos": float(pontos.replace(",", ".")),
        "status": "pontuou",
        "contexto": contexto,
        "placar_exibido": f"{gols_a} x {gols_b}",
        "scouts": scouts,
    }

def section_entries(soup, titulo, contexto):
    heading = None
    for h in soup.find_all(["h2", "h3", "h4"]):
        if titulo.lower() in clean(h.get_text(" ", strip=True)).lower():
            heading = h
            break
    if not heading:
        return []

    entries, seen = [], set()
    for el in heading.next_elements:
        if isinstance(el, Tag) and el is not heading and el.name in ["h2", "h3", "h4"]:
            break
        if isinstance(el, Tag) and el.name == "a":
            txt = clean(el.get_text(" ", strip=True))
            if "(Rodada" in txt:
                parsed = parse_entry(txt, contexto)
                if parsed and parsed["rodada"] not in seen:
                    entries.append(parsed)
                    seen.add(parsed["rodada"])
    return entries

def metric(text, label):
    m = re.search(re.escape(label) + r"\s*:\s*(-?\d+(?:[.,]\d+)?)", text, re.I)
    return float(m.group(1).replace(",", ".")) if m else None

def integer_metric(text, label):
    m = re.search(re.escape(label) + r"\s*:\s*(\d+)", text, re.I)
    return int(m.group(1)) if m else None

def total_games(text):
    m = re.search(r"Total:\s*(\d+)\s+J\b", text, re.I)
    return int(m.group(1)) if m else None

def fetch_player(target, rodada_referencia, janela, timeout=25):
    r = requests.get(target["url"], headers=HEADERS, timeout=timeout)
    r.raise_for_status()

    final_url = r.url
    soup = BeautifulSoup(r.text, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    h2 = soup.find("h2")
    nome = clean(h2.get_text(" ", strip=True)) if h2 else None

    pos_clube = re.search(r"\((GOL|LAT|ZAG|MEI|ATA|TEC)\s*-\s*([^)]+)\)", text, re.I)
    posicao = pos_clube.group(1).upper() if pos_clube else None
    clube = clean(pos_clube.group(2)) if pos_clube else None
    atleta_id = athlete_id_from_url(final_url) or athlete_id_from_url(target["url"])

    inconsistencias = []
    if atleta_id != target["atleta_id"]:
        inconsistencias.append(
            f"ID esperado {target['atleta_id']}, recebido {atleta_id}"
        )
    if norm(nome) != norm(target["nome_indice"]):
        inconsistencias.append(
            f"nome esperado '{target['nome_indice']}', recebido '{nome}'"
        )
    if posicao != target["posicao_indice"]:
        inconsistencias.append(
            f"posição esperada {target['posicao_indice']}, recebida {posicao}"
        )
    if inconsistencias:
        raise ValueError("TRAVA DE IDENTIDADE: " + "; ".join(inconsistencias))

    casa = section_entries(soup, "Pontuações em Casa", "casa")
    fora = section_entries(soup, "Pontuações Fora", "fora")
    hist = casa + fora
    hist.sort(key=lambda x: x["rodada"])

    inicio = max(1, int(rodada_referencia) - int(janela))
    fim = max(0, int(rodada_referencia) - 1)
    by_round = {x["rodada"]: x for x in hist}
    historico = []

    for rodada in range(inicio, fim + 1):
        if rodada in by_round:
            historico.append(by_round[rodada])
        else:
            historico.append({
                "rodada": rodada,
                "pontos": None,
                "status": "sem_registro",
                "contexto": None,
                "placar_exibido": None,
                "scouts": {},
            })

    return {
        "atleta_id": atleta_id,
        "nome": nome,
        "clube": clube,
        "posicao": posicao,
        "identidade_validada": True,
        "jogos_temporada": total_games(text),
        "preco": metric(text, "Preço"),
        "medias": {
            "geral": metric(text, "Média"),
            "casa": metric(text, "Média em Casa"),
            "fora": metric(text, "Média Fora"),
        },
        "historico": historico,
        "fonte_url": final_url,
    }

def label_player(p):
    return f"{p['nome_indice']} — {p['posicao_indice']} — ID {p['atleta_id']}"

st.set_page_config(page_title="MAC-Coletor 1.2", page_icon="⚽", layout="centered")
st.title("⚽ MAC-Coletor 1.2")
st.caption("Descoberta automática dos atletas da rodada — uso 100% pelo celular")

if "descoberta" not in st.session_state:
    st.session_state["descoberta"] = None
if "mac_resultado" not in st.session_state:
    st.session_state["mac_resultado"] = None

rodada = st.number_input(
    "Rodada de referência",
    min_value=2, max_value=38, value=25, step=1
)
janela = st.slider(
    "Quantas rodadas anteriores guardar",
    min_value=5, max_value=24, value=12
)

st.write("### 1. Descobrir atletas")
if st.button("BUSCAR ATLETAS DA RODADA", type="primary", use_container_width=True):
    try:
        with st.spinner("Lendo a lista de atletas da fonte..."):
            rodada_detectada, jogadores = discover_players()
        st.session_state["descoberta"] = {
            "rodada_detectada": rodada_detectada,
            "jogadores": jogadores,
        }
        st.session_state["mac_resultado"] = None
    except Exception as e:
        st.error(f"Falha ao descobrir atletas: {e}")

desc = st.session_state.get("descoberta")

if desc:
    rodada_detectada = desc["rodada_detectada"]
    jogadores = desc["jogadores"]

    if rodada_detectada is not None:
        if rodada_detectada == int(rodada):
            st.success(
                f"Fonte detectada na rodada {rodada_detectada}. "
                f"{len(jogadores)} atletas encontrados."
            )
        else:
            st.warning(
                f"A fonte está mostrando a rodada {rodada_detectada}, "
                f"mas você selecionou a rodada {int(rodada)}. "
                "Não colete até conferir a rodada."
            )
    else:
        st.warning(
            f"{len(jogadores)} atletas encontrados, mas não consegui confirmar "
            "automaticamente o número da rodada na página."
        )

    contagem = {}
    for p in jogadores:
        contagem[p["posicao_indice"]] = contagem.get(p["posicao_indice"], 0) + 1
    resumo = " | ".join(
        f"{pos}: {contagem.get(pos, 0)}"
        for pos in ["GOL", "LAT", "ZAG", "MEI", "ATA"]
    )
    st.caption(resumo)

    st.write("### 2. Escolher posições")
    posicoes = st.multiselect(
        "Posições para coletar",
        ["GOL", "LAT", "ZAG", "MEI", "ATA"],
        default=["GOL", "LAT", "ZAG", "MEI", "ATA"],
    )

    candidatos = [p for p in jogadores if p["posicao_indice"] in posicoes]

    modo = st.radio(
        "Modo de seleção",
        ["Todos das posições escolhidas", "Escolher jogadores manualmente"],
        horizontal=False,
    )

    if modo == "Escolher jogadores manualmente":
        mapa = {label_player(p): p for p in candidatos}
        escolhidos_labels = st.multiselect(
            "Jogadores",
            list(mapa.keys()),
            default=[],
        )
        escolhidos = [mapa[x] for x in escolhidos_labels]
    else:
        escolhidos = candidatos

    st.info(
        f"{len(escolhidos)} atleta(s) serão coletados. "
        "A trava valida ID + nome + posição na página individual. "
        "Ausência de registro continua sendo null, nunca zero automático."
    )

    st.write("### 3. Gerar arquivo do MAC")
    pode_coletar = bool(escolhidos) and (
        rodada_detectada is None or rodada_detectada == int(rodada)
    )

    if st.button(
        "COLETAR HISTÓRICOS",
        type="primary",
        use_container_width=True,
        disabled=not pode_coletar,
    ):
        resultados = []
        erros = []
        progresso = st.progress(0)
        status = st.empty()

        max_workers = min(6, max(1, len(escolhidos)))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    fetch_player, p, int(rodada), int(janela)
                ): p
                for p in escolhidos
            }

            concluidos = 0
            for future in as_completed(futures):
                p = futures[future]
                try:
                    resultados.append(future.result())
                except Exception as e:
                    erros.append({
                        "atleta_id": p["atleta_id"],
                        "nome": p["nome_indice"],
                        "posicao": p["posicao_indice"],
                        "erro": str(e),
                    })
                concluidos += 1
                progresso.progress(concluidos / len(escolhidos))
                status.write(
                    f"Coletados {concluidos}/{len(escolhidos)}..."
                )

        resultados.sort(key=lambda x: (x["posicao"], norm(x["nome"])))

        payload = {
            "schema": "MAC_COLETOR_1.2",
            "modo": "descoberta_automatica",
            "rodada_referencia": int(rodada),
            "rodada_detectada_na_fonte": rodada_detectada,
            "fonte": "Cartola FC Brasil",
            "coletado_em_utc": datetime.now(timezone.utc).isoformat(),
            "regra_ausencia": "ausencia_na_fonte_nao_e_zero",
            "regra_identidade": "ID+nome+posicao; clube confirmado na pagina individual",
            "posicoes_coletadas": posicoes,
            "total_descoberto": len(jogadores),
            "total_solicitado": len(escolhidos),
            "total_coletado": len(resultados),
            "total_erros": len(erros),
            "jogadores": resultados,
            "erros": erros,
        }
        st.session_state["mac_resultado"] = payload

payload = st.session_state.get("mac_resultado")

if payload:
    st.divider()
    st.write("### Resultado")
    st.success(
        f"{payload['total_coletado']} coletados | "
        f"{payload['total_erros']} erro(s)"
    )

    if payload["erros"]:
        st.error(
            "Existem atletas que falharam na coleta/trava. "
            "O arquivo registra todos os erros para auditoria."
        )
        with st.expander("Ver erros"):
            st.json(payload["erros"])

    por_posicao = {}
    for p in payload["jogadores"]:
        por_posicao[p["posicao"]] = por_posicao.get(p["posicao"], 0) + 1
    st.caption(
        " | ".join(
            f"{pos}: {por_posicao.get(pos, 0)}"
            for pos in ["GOL", "LAT", "ZAG", "MEI", "ATA"]
        )
    )

    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "BAIXAR ARQUIVO PARA O MAC",
        data=json_bytes,
        file_name=f"MAC_DADOS_R{payload['rodada_referencia']}_V1_2.json",
        mime="application/json",
        use_container_width=True,
    )

    with st.expander("Ver amostra dos primeiros 10 atletas"):
        for p in payload["jogadores"][:10]:
            st.write(
                f"**{p['nome']}** — {p['posicao']} / {p['clube']} "
                f"— ID {p['atleta_id']} — Média {p['medias']['geral']}"
            )
