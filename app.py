
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
import streamlit as st
from bs4 import BeautifulSoup, Tag

API_BASE = "https://api.cartola.globo.com"
SITE_BASE = "https://www.cartolafcbrasil.com.br"

POS_MAP = {1: "GOL", 2: "LAT", 3: "ZAG", 4: "MEI", 5: "ATA", 6: "TEC"}
STATUS_MAP = {
    2: "Dúvida",
    3: "Suspenso",
    5: "Contundido",
    6: "Nulo",
    7: "Provável",
}

CLUBE_SLUG = {
    "FLA": "flamengo", "PAL": "palmeiras", "SAN": "santos",
    "COR": "corinthians", "SAO": "sao-paulo", "BOT": "botafogo",
    "FLU": "fluminense", "VAS": "vasco", "CAM": "atletico-mg",
    "CRU": "cruzeiro", "GRE": "gremio", "INT": "internacional",
    "BAH": "bahia", "VIT": "vitoria", "CAP": "athletico-pr",
    "RBB": "bragantino", "CFC": "coritiba", "CHA": "chapecoense",
    "MIR": "mirassol", "REM": "remo",
}

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

def slugify(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

def get_json(url, timeout=25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

def carregar_mercado():
    status = get_json(f"{API_BASE}/mercado/status")
    mercado = get_json(f"{API_BASE}/atletas/mercado")
    clubes = get_json(f"{API_BASE}/clubes")

    club_map = {}
    if isinstance(clubes, dict):
        for k, v in clubes.items():
            if isinstance(v, dict):
                try:
                    cid = int(v.get("id", k))
                except Exception:
                    continue
                club_map[cid] = v

    atletas = mercado.get("atletas", mercado if isinstance(mercado, list) else [])
    return status, atletas, club_map

def atleta_para_alvo(a, club_map):
    clube = club_map.get(int(a["clube_id"]), {})
    abreviacao = clube.get("abreviacao") or clube.get("abreviacao_nome") or ""
    nome_clube = clube.get("nome") or ""
    apelido = clean(a.get("apelido") or a.get("nome") or "")
    posicao = POS_MAP.get(int(a.get("posicao_id", 0)))
    status_id = int(a.get("status_id", 0))
    status_nome = STATUS_MAP.get(status_id, f"Status {status_id}")

    club_slug = CLUBE_SLUG.get(abreviacao) or slugify(nome_clube)
    atleta_slug = a.get("slug") or slugify(apelido)
    atleta_id = str(a["atleta_id"])

    return {
        "atleta_id": atleta_id,
        "nome_mercado": apelido,
        "clube": nome_clube,
        "clube_abreviacao": abreviacao,
        "posicao": posicao,
        "status": status_nome,
        "status_id": status_id,
        "preco_mercado": a.get("preco_num"),
        "media_mercado": a.get("media_num"),
        "jogos_mercado": a.get("jogos_num"),
        "url": f"{SITE_BASE}/scouts/cartola-fc-2026/atleta/{club_slug}/{atleta_id}/{atleta_slug}",
    }

def parse_entry(text, contexto):
    text = clean(text)
    m = re.search(
        r"(\d+)\s*x\s*(\d+)\s*\(Rodada\s*#\s*(\d+)\)\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$",
        text, re.I
    )
    if not m:
        return None

    gols_a, gols_b, rodada, pontos, resto = m.groups()
    tokens = resto.split()
    scouts = {}
    i = 0

    while i + 1 < len(tokens):
        if re.fullmatch(r"-?\d+(?:[.,]\d+)?", tokens[i]) and tokens[i+1].upper() in SCOUT_CODES:
            qtd = float(tokens[i].replace(",", "."))
            if qtd.is_integer():
                qtd = int(qtd)
            scouts[tokens[i+1].upper()] = qtd
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
    nome_site = clean(h2.get_text(" ", strip=True)) if h2 else None

    pos_clube = re.search(r"\((GOL|LAT|ZAG|MEI|ATA|TEC)\s*-\s*([^)]+)\)", text, re.I)
    posicao_site = pos_clube.group(1).upper() if pos_clube else None
    clube_site = clean(pos_clube.group(2)) if pos_clube else None

    m = re.search(r"/atleta/[^/]+/(\d+)/", final_url)
    id_url = m.group(1) if m else None

    inconsistencias = []
    if id_url != target["atleta_id"]:
        inconsistencias.append(f"ID mercado {target['atleta_id']} / site {id_url}")
    if posicao_site != target["posicao"]:
        inconsistencias.append(f"posição mercado {target['posicao']} / site {posicao_site}")

    n1, n2 = norm(target["nome_mercado"]), norm(nome_site)
    if n1 and n2 and not (n1 == n2 or n1 in n2 or n2 in n1):
        inconsistencias.append(
            f"nome mercado '{target['nome_mercado']}' / site '{nome_site}'"
        )

    if inconsistencias:
        raise ValueError("TRAVA DE IDENTIDADE: " + "; ".join(inconsistencias))

    casa = section_entries(soup, "Pontuações em Casa", "casa")
    fora = section_entries(soup, "Pontuações Fora", "fora")
    hist = sorted(casa + fora, key=lambda x: x["rodada"])

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
        "atleta_id": target["atleta_id"],
        "nome": nome_site or target["nome_mercado"],
        "clube": clube_site or target["clube"],
        "clube_abreviacao": target["clube_abreviacao"],
        "posicao": target["posicao"],
        "status_mercado": target["status"],
        "identidade_validada": True,
        "jogos_temporada": total_games(text) or target["jogos_mercado"],
        "preco": metric(text, "Preço") or target["preco_mercado"],
        "medias": {
            "geral": metric(text, "Média"),
            "casa": metric(text, "Média em Casa"),
            "fora": metric(text, "Média Fora"),
        },
        "mercado_oficial": {
            "media": target["media_mercado"],
            "jogos": target["jogos_mercado"],
            "preco": target["preco_mercado"],
        },
        "historico": historico,
        "fonte_url": final_url,
    }

def label_player(p):
    return (
        f"{p['nome_mercado']} — {p['posicao']} / "
        f"{p['clube_abreviacao']} — {p['status']} — ID {p['atleta_id']}"
    )

st.set_page_config(page_title="MAC-Coletor 1.3", page_icon="⚽", layout="centered")
st.title("⚽ MAC-Coletor 1.3")
st.caption("Elenco completo do mercado + busca livre por atleta")

if "mercado" not in st.session_state:
    st.session_state["mercado"] = None
if "mac_resultado" not in st.session_state:
    st.session_state["mac_resultado"] = None

rodada = st.number_input("Rodada de referência", min_value=2, max_value=38, value=25, step=1)
janela = st.slider("Quantas rodadas anteriores guardar", min_value=5, max_value=24, value=12)

st.write("### 1. Carregar elenco completo")
if st.button("CARREGAR JOGADORES DO CARTOLA", type="primary", use_container_width=True):
    try:
        with st.spinner("Buscando o mercado oficial completo..."):
            status, atletas_api, clubes = carregar_mercado()
            alvos = []
            for a in atletas_api:
                pos = POS_MAP.get(int(a.get("posicao_id", 0)))
                if pos in {"GOL", "LAT", "ZAG", "MEI", "ATA"}:
                    alvos.append(atleta_para_alvo(a, clubes))

        st.session_state["mercado"] = {
            "rodada_atual": int(status.get("rodada_atual", 0)),
            "status_mercado": int(status.get("status_mercado", 0)),
            "temporada": int(status.get("temporada", 2026)),
            "atletas": alvos,
        }
        st.session_state["mac_resultado"] = None
    except Exception as e:
        st.error(f"Falha ao carregar mercado oficial: {e}")

mercado = st.session_state.get("mercado")

if mercado:
    atletas = mercado["atletas"]
    rodada_oficial = mercado["rodada_atual"]

    st.success(
        f"{len(atletas)} jogadores carregados do mercado oficial "
        f"da rodada {rodada_oficial}."
    )

    if rodada_oficial != int(rodada):
        st.warning(
            f"O Cartola oficial está na rodada {rodada_oficial}, "
            f"mas o app está configurado para {int(rodada)}."
        )

    st.write("### 2. Escolher quem pesquisar")

    modo = st.radio(
        "Como você quer pesquisar?",
        [
            "Só prováveis",
            "Buscar qualquer jogador do elenco",
            "Todos os jogadores filtrados",
        ],
    )

    if modo == "Só prováveis":
        candidatos = [p for p in atletas if p["status"] == "Provável"]

        posicoes = st.multiselect(
            "Posições",
            ["GOL", "LAT", "ZAG", "MEI", "ATA"],
            default=["GOL", "LAT", "ZAG", "MEI", "ATA"],
        )
        candidatos = [p for p in candidatos if p["posicao"] in posicoes]

        mapa = {label_player(p): p for p in candidatos}
        labels = st.multiselect(
            "Prováveis disponíveis",
            list(mapa.keys()),
            default=[],
        )
        escolhidos = [mapa[x] for x in labels]

    elif modo == "Buscar qualquer jogador do elenco":
        clubes_disponiveis = sorted(
            {p["clube"] for p in atletas if p["clube"]},
            key=norm
        )
        clube_filtro = st.selectbox(
            "Clube",
            ["Todos"] + clubes_disponiveis,
        )

        posicao_filtro = st.selectbox(
            "Posição",
            ["Todas", "GOL", "LAT", "ZAG", "MEI", "ATA"],
        )

        busca = st.text_input(
            "Digite parte do nome do jogador",
            placeholder="Ex.: Neymar, Marcelinho, Gabriel..."
        )

        candidatos = atletas
        if clube_filtro != "Todos":
            candidatos = [p for p in candidatos if p["clube"] == clube_filtro]
        if posicao_filtro != "Todas":
            candidatos = [p for p in candidatos if p["posicao"] == posicao_filtro]
        if busca.strip():
            q = norm(busca)
            candidatos = [p for p in candidatos if q in norm(p["nome_mercado"])]

        mapa = {label_player(p): p for p in candidatos}
        labels = st.multiselect(
            f"Resultados ({len(candidatos)})",
            list(mapa.keys()),
            default=[],
        )
        escolhidos = [mapa[x] for x in labels]

        st.caption(
            "Aqui aparecem também jogadores em dúvida, suspensos, "
            "contundidos ou com outro status — desde que estejam no mercado oficial."
        )

    else:
        status_disponiveis = sorted({p["status"] for p in atletas})
        status_filtro = st.multiselect(
            "Status",
            status_disponiveis,
            default=status_disponiveis,
        )
        posicoes = st.multiselect(
            "Posições",
            ["GOL", "LAT", "ZAG", "MEI", "ATA"],
            default=["GOL", "LAT", "ZAG", "MEI", "ATA"],
        )
        clubes_disponiveis = sorted(
            {p["clube"] for p in atletas if p["clube"]},
            key=norm
        )
        clubes_filtro = st.multiselect(
            "Clubes",
            clubes_disponiveis,
            default=[],
            help="Vazio = todos os clubes",
        )

        escolhidos = [
            p for p in atletas
            if p["status"] in status_filtro
            and p["posicao"] in posicoes
            and (not clubes_filtro or p["clube"] in clubes_filtro)
        ]

    st.info(f"{len(escolhidos)} jogador(es) selecionado(s) para coleta.")

    st.write("### 3. Coletar históricos")
    pode = bool(escolhidos) and rodada_oficial == int(rodada)

    if st.button(
        "GERAR ARQUIVO DO MAC",
        type="primary",
        use_container_width=True,
        disabled=not pode,
    ):
        resultados, erros = [], []
        progresso = st.progress(0)
        status_box = st.empty()

        max_workers = min(6, max(1, len(escolhidos)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fetch_player, p, int(rodada), int(janela)): p
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
                        "nome": p["nome_mercado"],
                        "clube": p["clube"],
                        "posicao": p["posicao"],
                        "status": p["status"],
                        "url_tentada": p["url"],
                        "erro": str(e),
                    })
                concluidos += 1
                progresso.progress(concluidos / len(escolhidos))
                status_box.write(f"Processados {concluidos}/{len(escolhidos)}...")

        resultados.sort(key=lambda x: (x["posicao"], norm(x["nome"])))

        payload = {
            "schema": "MAC_COLETOR_1.3",
            "modo": "elenco_completo_busca_livre",
            "rodada_referencia": int(rodada),
            "rodada_oficial": rodada_oficial,
            "fonte_lista_atletas": "API oficial Cartola",
            "fonte_historico": "Cartola FC Brasil",
            "coletado_em_utc": datetime.now(timezone.utc).isoformat(),
            "regra_ausencia": "ausencia_na_fonte_nao_e_zero",
            "regra_identidade": "ID oficial + nome + posição",
            "total_mercado": len(atletas),
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
        with st.expander("Ver erros"):
            st.json(payload["erros"])

    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "BAIXAR ARQUIVO PARA O MAC",
        data=json_bytes,
        file_name=f"MAC_DADOS_R{payload['rodada_referencia']}_V1_3.json",
        mime="application/json",
        use_container_width=True,
    )

    with st.expander("Ver jogadores coletados"):
        for p in payload["jogadores"]:
            st.write(
                f"**{p['nome']}** — {p['posicao']} / {p['clube']} "
                f"— {p['status_mercado']} — ID {p['atleta_id']}"
            )
