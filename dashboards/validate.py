#!/usr/bin/env python3
"""validate.py — valida o dashboard do lab sem precisar de Grafana de pé.

Por que isto existe: dashboard quebrado só se descobre na hora em que alguém
precisa dele, que é exatamente a hora em que não dá para descobrir. As checagens
abaixo pegam as cinco formas reais com que um JSON de dashboard apodrece:

  1. estrutura: chaves obrigatórias, ids únicos, `gridPos` que se sobrepõe;
  2. painel de consulta sem `datasource`, ou alvo sem `refId`/`expr`;
  3. **métrica digitada errado** — a causa nº 1 de painel "vazio mas sem erro";
  4. **label inexistente** no seletor (filtro que não filtra nada);
  5. variável de template usada mas não declarada (funciona no seu navegador,
     quebra no painel provisionado).

As métricas e labels permitidos vêm de uma lista **derivada do código de
instrumentação e do config do Collector** (`--verificar-fonte`, ativo por padrão):
se alguém renomear `sre.jornada.duration` no `src/`, o dashboard é reprovado aqui,
e não três semanas depois numa revisão de incidente.

Isto **não** é um validador de esquema do Grafana. Ele não garante que o painel
fica bonito, nem que a consulta é barata. Para o esquema, use a API de
provisionamento ou `grafana dashboard validate` quando disponível.

Uso:
    python3 dashboards/validate.py                      # todos os JSON da pasta
    python3 dashboards/validate.py dashboards/x.json
    python3 dashboards/validate.py --listar-metricas
    python3 dashboards/validate.py --json | --strict | --sem-verificar-fonte
    make dashboards          # atalho na raiz, se houver Makefile

Códigos de saída: 0 ok · 1 violação · 2 uso incorreto/arquivo ilegível
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)

CHAVES_OBRIGATORIAS = ("title", "uid", "schemaVersion", "version", "panels",
                       "templating", "time", "refresh", "timezone", "tags", "editable")
PAINES_SEM_QUERY = {"text", "row", "collapse", "library_panel", "guide"}
VARIAVEIS_GRAFANA = {"__rate_interval", "__interval", "__interval_ms", "__from", "__to",
                     "__timeFilter", "__unixEpochFilter", "__dashboard", "__user", "__name",
                     "__elements", "__json", "__value", "__series.name"}

# ---- vocabulário do lab: é isto que o código instrumenta, nada além ----------
METRICAS_PERMITIDAS = {
    # OTLP -> Collector -> exporter Prometheus (histogramas viram _bucket/_sum/_count)
    "sre_jornada_duration_seconds",
    "sre_fronteira_relogio_skew_seconds",
    # texto Prometheus exposto pela própria fronteira (scrape direto, sem OTel)
    "fronteira_correlacao_total",
    "fronteira_requisicoes_total",
    "fronteira_rc_erro_total",
    # infra
    "up", "scrape_samples_scraped", "scrape_duration_seconds", "target_info",
}
SUFIXOS_DE_HISTOGRAMA = ("_bucket", "_sum", "_count")

LABELS_PERMITIDOS = {
    "jornada", "etapa", "camada", "resultado", "fronteira_propagou", "situacao",
    "le", "quantile", "service_name", "service_namespace", "deployment_environment_name",
    "cloud_region", "lab", "job", "instance", "team", "caminho", "service",
    "boundary_known", "severity", "__name__",
}

# funções e operadores PromQL: aparecem como identificadores e não são séries
FUNCOES_PROMQL = {
    "sum", "avg", "min", "max", "count", "count_values", "quantile", "topk", "bottomk",
    "rate", "irate", "increase", "delta", "idelta", "histogram_quantile", "histogram_mean",
    "histogram_count", "histogram_sum", "abs", "absent", "ceil", "floor", "round", "clamp",
    "clamp_min", "clamp_max", "day_of_month", "day_of_week", "days_in_month", "hour",
    "minute", "month", "year", "timestamp", "time", "vector", "scalar", "sort", "sort_desc",
    "label_replace", "label_join", "by", "without", "on", "ignoring", "group_left",
    "group_right", "offset", "bool", "and", "or", "unless", "inf", "nan", "start", "end",
    "step", "absent_over_time", "avg_over_time", "max_over_time", "min_over_time",
    "sum_over_time", "count_over_time", "stddev_over_time", "stdvar_over_time",
    "last_over_time", "present_over_time", "quantile_over_time", "range_over_time",
    "pi", "ln", "log2", "log10", "exp", "sqrt", "sgn", "deg", "rad",
    # chamadas usadas em variáveis de template do Grafana, não em PromQL de painel
    "label_values", "label_names", "label_values_query", "metrics", "series",
}

# ---- LogQL: o painel de log usa isto, não PromQL ----------------------------
FUNCOES_LOGQL = {"json", "logfmt", "regexp", "line_format", "label_format", "unwrap",
                 "count_over_time", "rate", "sum", "avg", "last_over_time", "first_over_time",
                 "bytes_rate", "bytes_over_time", "absent_over_time", "vector", "quantile_over_time",
                 "pattern", "detected_fields", "duration_seconds", "bytes", "err"}

TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
VAR_USO = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
SERIE_EM_EXPR = re.compile(r"(?<![A-Za-z0-9_:])([a-zA-Z_][a-zA-Z0-9_]*)(?![A-Za-z0-9_]*\s*\()")


def limpar_texto_consulta(expr: str) -> str:
    """Normaliza a consulta antes de tokenizar.

    Remove três coisas que o tokenizador ignorante de sintaxe confundiria com
    nome de série: literal entre aspas, variável (`$__rate_interval`, `$jornada`)
    e seletor de intervalo de duração (`[10m]`, `[5m]`) - o `m` de minutos não é
    métrica nenhuma, e tratar como tal gera falso positivo, que é a maneira mais
    rápida de fazer a ferramenta perder a credibilidade.
    """
    sem_strings = re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)
    sem_strings = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sem_strings)
    sem_strings = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", " ", sem_strings)
    sem_strings = re.sub(r"\[\s*\d+[a-zA-Z]+\s*\]", "[]", sem_strings)
    return sem_strings


def balanceado(expr: str) -> str | None:
    limpo = limpar_texto_consulta(expr)
    for abre, fecha in (("(", ")"), ("{", "}"), ("[", "]")):
        if limpo.count(abre) != limpo.count(fecha):
            return f"{abre}{fecha} desbalanceados"
    return None


def extrair_nomes(expr: str, linguagem: str) -> tuple[set[str], set[str]]:
    """Retorna (candidatos a série, candidatos a label) encontrados na expressão."""
    limpo = limpar_texto_consulta(expr)
    nomes = set(TOKEN.findall(limpo))
    # labels: nomes seguidos de operador de matcher dentro de chaves
    labels = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)", limpo))
    series = set()
    for nome in nomes:
        if nome in FUNCOES_PROMQL or nome in FUNCOES_LOGQL or nome in labels:
            continue
        if nome in {"And", "By", "Or"}:
            continue
        series.add(nome)
    return series, labels


def verificar_fonte(repositorio: str, metricas_usadas: set[str]) -> list[str]:
    """Cada série do dashboard precisa existir no código ou na configuração.

    É a checagem que transforma 'o painel está ok' em 'o painel está ok com o
    que este repositório instrumenta hoje'.
    """
    corpo = []
    for subpasta in ("src", "config", "../src", "../config"):
        pasta = os.path.join(repositorio, subpasta)
        if not os.path.isdir(pasta):
            continue
        for nome in sorted(os.listdir(pasta)):
            if not nome.endswith((".py", ".yaml", ".yml")):
                continue
            try:
                corpo.append(open(os.path.join(pasta, nome), encoding="utf-8").read())
            except OSError:
                continue
    texto = "\n".join(corpo)
    erros = []
    for metrica in sorted(metricas_usadas):
        base = metrica
        for sufixo in SUFIXOS_DE_HISTOGRAMA + ("_total",):
            if base.endswith(sufixo):
                base = base[: -len(sufixo)]
                break
        candidatos = {metrica, base, base.replace("_", "."), f"{base}_seconds", f"{base}_total"}
        if not any(c and c in texto for c in candidatos):
            erros.append(f"série `{metrica}` não aparece em src/ nem em config/ "
                         f"(dashboard e instrumentação divergiram?)")
    return erros


def validar_dashboard(caminho: str, verificar_fonte_flag: bool = True) -> tuple[list[str], list[str], dict]:
    erros: list[str] = []
    avisos: list[str] = []
    nome = _relativo(caminho)

    try:
        with open(caminho, encoding="utf-8") as fh:
            dash = json.load(fh)
    except FileNotFoundError:
        return [f"{caminho}: arquivo não encontrado"], [], {}
    except json.JSONDecodeError as exc:
        return [f"{caminho}: JSON inválido (linha {exc.lineno}, coluna {exc.colno}): {exc.msg}"], [], {}

    if not isinstance(dash, dict):
        return [f"{nome}: raiz do JSON precisa ser um objeto"], [], {}

    for chave in CHAVES_OBRIGATORIAS:
        if chave not in dash:
            erros.append(f"{nome}: chave obrigatória ausente: `{chave}`")

    uid = dash.get("uid", "")
    if isinstance(uid, str) and uid:
        if "${" in uid or "$" in uid:
            erros.append(f"{nome}: `uid` não pode conter variável ({uid!r})")
        elif not re.fullmatch(r"[A-Za-z0-9\-_]{3,40}", uid):
            erros.append(f"{nome}: `uid` fora do padrão aceito pelo Grafana: {uid!r}")
    schema = dash.get("schemaVersion")
    if not isinstance(schema, int):
        erros.append(f"{nome}: `schemaVersion` deve ser inteiro, veio {schema!r}")
    elif schema < 36:
        avisos.append(f"{nome}: schemaVersion {schema} é antigo; painéis novos do Grafana 11+ usam 39-41")

    painel_list = dash.get("panels")
    if not isinstance(painel_list, list) or not painel_list:
        erros.append(f"{nome}: `panels` vazio ou ausente")
        painel_list = []

    # ---- variáveis de template --------------------------------------------
    declaradas: dict[str, str] = {}
    templating = dash.get("templating") or {}
    for var in (templating.get("list") or []):
        if not isinstance(var, dict) or "name" not in var:
            erros.append(f"{nome}: variável de template sem `name`")
            continue
        declaradas[var["name"]] = var.get("type", "")
        if var.get("type") == "datasource" and var.get("query") not in ("prometheus", "loki", "tempo", "cloud-monitoring"):
            erros.append(f"{nome}: variável de datasource com `query` inesperada: {var.get('query')!r}")
        if var.get("type") == "query" and not isinstance(var.get("datasource"), dict):
            erros.append(f"{nome}: variável `{var['name']}` do tipo query sem datasource")
    if "DS_PROM" not in declaradas and "DS_PROMETHEUS" not in declaradas:
        avisos.append(f"{nome}: nenhuma variável de datasource Prometheus declarada")

    # ---- painéis -----------------------------------------------------------
    ids_vistos: dict[int, str] = {}
    ocupacao: dict[tuple[int, int], int] = {}
    metricas_usadas: set[str] = set()
    labels_usados: set[str] = set()

    for painel in painel_list:
        if not isinstance(painel, dict):
            erros.append(f"{nome}: painel precisa ser objeto")
            continue
        pid = painel.get("id")
        titulo = painel.get("title") or f"(painel {pid})"
        tipo = painel.get("type") or "?"
        onde = f"{nome}: painel {pid} [{tipo}] `{titulo}`"

        if not isinstance(pid, int):
            erros.append(f"{onde}: `id` ausente ou não inteiro")
        elif pid in ids_vistos:
            erros.append(f"{onde}: `id` duplicado (repete o painel {ids_vistos[pid]})")
        else:
            ids_vistos[pid] = titulo
        if not painel.get("title"):
            erros.append(f"{onde}: painel sem título")
        if not painel.get("type"):
            erros.append(f"{onde}: painel sem `type`")

        pos = painel.get("gridPos") or {}
        if not all(chave in pos for chave in ("h", "w", "x", "y")):
            erros.append(f"{onde}: `gridPos` incompleto: {pos}")
        else:
            x, y, w, h = (int(pos[k]) for k in ("x", "y", "w", "h"))
            if w < 1 or h < 1:
                erros.append(f"{onde}: gridPos com w/h não positivo")
            if x < 0 or y < 0:
                erros.append(f"{onde}: gridPos com coordenada negativa")
            if x + w > 24:
                erros.append(f"{onde}: gridPos ultrapassa as 24 colunas (x={x} + w={w} = {x + w})")
            dono = pid if isinstance(pid, int) else -1
            sobreposicoes = 0
            for linha in range(y, y + h):
                for coluna in range(x, x + w):
                    chave = (coluna, linha)
                    anterior = ocupacao.get(chave)
                    if anterior is not None and anterior != dono:
                        # uma mensagem por painel: 96 linhas de "sobrepõe" pelo mesmo
                        # gridPos não ajuda ninguém a consertar
                        if sobreposicoes < 1:
                            erros.append(f"{onde}: gridPos sobrepõe o painel {anterior} na célula ({coluna},{linha})")
                        sobreposicoes += 1
                    else:
                        ocupacao[chave] = dono

        if tipo in PAINES_SEM_QUERY:
            continue

        ds = painel.get("datasource")
        if not isinstance(ds, dict) or not ds.get("uid"):
            erros.append(f"{onde}: painel de consulta sem `datasource.uid`")
            uid_ds = None
        else:
            uid_ds = ds["uid"]
            if isinstance(uid_ds, str):
                m = VAR_USO.fullmatch(uid_ds)
                if m and m.group(1) not in declaradas:
                    erros.append(f"{onde}: datasource aponta para variável não declarada {uid_ds}")
                elif m and declaradas[m.group(1)] != "datasource":
                    erros.append(f"{onde}: variável {uid_ds} não é do tipo datasource")

        alvos = painel.get("targets")
        if not isinstance(alvos, list) or not alvos:
            erros.append(f"{onde}: painel de consulta sem `targets`")
            continue
        refs: set[str] = set()
        for alvo in alvos:
            if not isinstance(alvo, dict):
                erros.append(f"{onde}: target precisa ser objeto")
                continue
            ref = alvo.get("refId")
            if not ref:
                erros.append(f"{onde}: target sem `refId`")
            elif ref in refs:
                erros.append(f"{onde}: `refId` duplicado no painel: {ref}")
            else:
                refs.add(str(ref))
            expr = alvo.get("expr") or alvo.get("query")
            if not isinstance(expr, str) or not expr.strip():
                erros.append(f"{onde}: target {ref} sem expressão")
                continue
            problema = balanceado(expr)
            if problema:
                erros.append(f"{onde}: target {ref}: {problema} em `{expr[:90]}`")
            alvo_ds = alvo.get("datasource")
            if isinstance(alvo_ds, dict) and isinstance(uid_ds, str) and alvo_ds.get("uid") not in (uid_ds, None):
                erros.append(f"{onde}: target {ref} aponta para datasource diferente do painel")

            linguagem = "logql" if (isinstance(uid_ds, str) and "LOKI" in uid_ds.upper()) else "promql"
            if tipo == "logs" or (isinstance(alvo.get("queryType"), str)):
                linguagem = "logql" if tipo == "logs" else linguagem
            series, labels = extrair_nomes(expr, linguagem)
            for variavel in VAR_USO.findall(expr):
                if variavel not in declaradas and variavel not in VARIAVEIS_GRAFANA:
                    erros.append(f"{onde}: target {ref} usa ${variavel}, não declarada em `templating`")
            if linguagem == "promql":
                for serie in series:
                    base = serie
                    for sufixo in SUFIXOS_DE_HISTOGRAMA + ("_total",):
                        if base.endswith(sufixo):
                            base = base[: -len(sufixo)]
                            break
                    if serie in METRICAS_PERMITIDAS or base in METRICAS_PERMITIDAS:
                        metricas_usadas.add(base)
                        continue
                    if serie in LABELS_PERMITIDOS:
                        continue
                    erros.append(f"{onde}: target {ref} referencia série desconhecida `{serie}` "
                                 f"(não está em METRICAS_PERMITIDAS nem no vocabulário do lab)")
            for label in labels:
                if label not in LABELS_PERMITIDOS:
                    erros.append(f"{onde}: target {ref} filtra por label `{label}`, fora do vocabulário declarado")
                labels_usados.add(label)

    # expressões das variáveis de template também precisam bater com o vocabulário
    for var in (templating.get("list") or []):
        definicao = var.get("definition") or ""
        if not isinstance(definicao, str):
            continue
        chamada = re.match(r"\s*label_values\s*\(\s*([^,()]+)", definicao)
        if chamada:
            # label_values(<serie>, <label>): só o primeiro argumento é série.
            serie = chamada.group(1).strip().split("{")[0].strip()
            if serie:
                base = serie
                for sufixo in SUFIXOS_DE_HISTOGRAMA:
                    if base.endswith(sufixo):
                        base = base[: -len(sufixo)]
                        break
                if serie in METRICAS_PERMITIDAS or base in METRICAS_PERMITIDAS:
                    metricas_usadas.add(base)
                else:
                    erros.append(f"{nome}: variável `{var.get('name')}` usa série desconhecida `{serie}`")
        else:
            for serie, _ in [extrair_nomes(definicao, "promql")]:
                for nome_serie in serie:
                    if nome_serie in {"prometheus", "loki", "tempo"}:
                        continue
                    erros.append(f"{nome}: variável `{var.get('name')}` referencia `{nome_serie}`, "
                                 f"fora do vocabulário do lab")

    if verificar_fonte_flag:
        erros.extend(f"{nome}: {e}" for e in verificar_fonte(RAIZ, metricas_usadas))

    estatisticas = {
        "arquivo": nome,
        "uid": uid,
        "schemaVersion": schema,
        "paineis": len(painel_list),
        "paineis_de_query": len(painel_list) - sum(1 for p in painel_list if p.get("type") in PAINES_SEM_QUERY),
        "variaveis": sorted(declaradas),
        "metricas_referenciadas": sorted(metricas_usadas),
        "labels_referenciados": sorted(labels_usados),
    }
    return erros, avisos, estatisticas


def _relativo(caminho: str) -> str:
    try:
        return os.path.relpath(caminho, RAIZ)
    except ValueError:
        return caminho


def principal(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Valida o JSON de dashboard do lab: estrutura, datasources, "
                    "targets, vocabulário de métricas/labels e coerência com src/ e config/. "
                    "Stdlib apenas, offline.",
    )
    parser.add_argument("arquivos", nargs="*", help="arquivos .json (padrão: todos em dashboards/)")
    parser.add_argument("--json", action="store_true", dest="saida_json", help="saída mecanicamente legível")
    parser.add_argument("--strict", action="store_true", help="aviso vira falha")
    parser.add_argument("--sem-verificar-fonte", action="store_true",
                        help="não confere métricas contra src/ e config/")
    parser.add_argument("--listar-metricas", action="store_true",
                        help="imprime o vocabulário permitido e sai")
    args = parser.parse_args(argv)

    if args.listar_metricas:
        print("MÉTRICAS PERMITIDAS:")
        for metrica in sorted(METRICAS_PERMITIDAS):
            print(f"  - {metrica}")
        print("LABELS PERMITIDOS:")
        print("  " + ", ".join(sorted(LABELS_PERMITIDOS)))
        return 0

    alvos = args.arquivos or sorted(
        os.path.join(AQUI, nome) for nome in os.listdir(AQUI) if nome.endswith(".json")
    )
    if not alvos:
        print("nenhum JSON para validar em dashboards/", file=sys.stderr)
        return 2

    todos_erros: list[str] = []
    todos_avisos: list[str] = []
    resumo: list[dict] = []
    for caminho in alvos:
        erros, avisos, estatisticas = validar_dashboard(caminho, not args.sem_verificar_fonte)
        todos_erros.extend(erros)
        todos_avisos.extend(avisos)
        if estatisticas:
            resumo.append(estatisticas)

    if args.saida_json:
        print(json.dumps({"ok": not todos_erros, "resumo": resumo,
                          "erros": todos_erros, "avisos": todos_avisos}, ensure_ascii=False, indent=2))
    else:
        for estatisticas in resumo:
            print(f"[ok  ] {estatisticas['arquivo']}  uid={estatisticas['uid']}  "
                  f"schemaVersion={estatisticas['schemaVersion']}  "
                  f"painéis={estatisticas['paineis']} (consulta={estatisticas['paineis_de_query']})")
            print(f"        variáveis: {', '.join(estatisticas['variaveis']) or '(nenhuma)'}")
            print(f"        métricas:  {', '.join(estatisticas['metricas_referenciadas'])}")
            print(f"        labels:    {', '.join(estatisticas['labels_referenciados'])}")
        for erro in todos_erros:
            print(f"[FAIL] {erro}")
        for aviso in todos_avisos:
            print(f"[warn] {aviso}")
        if not todos_erros:
            print(f"\n{len(resumo)} dashboard(s) validado(s): 0 erro(s), {len(todos_avisos)} aviso(s)")
        if args.strict and todos_avisos and not todos_erros:
            print("--strict: avisos contam como falha", file=sys.stderr)

    if args.strict and todos_avisos:
        return 1
    return 1 if todos_erros else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(principal())
