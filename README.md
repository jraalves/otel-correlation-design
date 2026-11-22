# Correlação de telemetria na fronteira com sistema transacional (lab)

[English](#english)

Como você acompanha **uma** jornada de pagamento que nasce em uma API em contêiner, passa por fila e
termina num sistema transacional que não fala OpenTelemetry, não fala HTTP e devolve um identificador
em outro formato?

Este repositório responde com código executável: uma borda instrumentada com OpenTelemetry, um
simulador de fronteira transacional com relógio próprio e formatos de ID hostis, um collector que
promove a chave de negócio a atributo de telemetria, e um dashboard do Grafana cujas **consultas são
validadas por um script offline** — porque painel que referencia métrica inexistente não falha na tela,
falha no incidente.

Nada aqui toca sistema real. Não há credencial, host, cliente ou dado de produção.

---

## O problema, em três frases

1. `traceparent` (W3C Trace Context) propaga contexto **entre componentes que instrumentados**. Um
   sistema legado transacional aceita um campo de 12 bytes, devolve outro, e não reemite `traceparent`.
2. Sem uma **chave de negócio estável**, o traço morre na fronteira: você tem dois meios de uma
   jornada e nenhuma junta entre eles.
3. O custo real não é "não ter trace"; é o tempo até alguém conseguir dizer **qual** das 3.000
   transações daquela janela falhou na travessia.

## Arquitetura

```
 cliente
   │  X-Correlation-Id + traceparent + baggage (sre.correlation_id, sre.jornada, sre.estagio)
   ▼
┌────────────────────┐  OTLP/HTTP   ┌──────────────────┐
│  api-gateway       │─────────────▶│  otel-collector  │
│  FastAPI (8080)    │              │  0.165.0 contrib │
└─────────┬──────────┘              └───┬────┬─────┬───┘
          │ HTTP + cabeçalhos           │    │     │
          ▼                             │    │     └─ logs  → Loki 3.5.1  (OTLP)
┌────────────────────┐                  │    └─────── metrics → Prometheus v3.16
│ fronteira          │                  └──────────── traces → Jaeger 1.76
│ transacional       │  /metricos exposto em texto (counter próprio, sem SDK)
│ (8090, relógio     │
│  deslocado 420 ms) │──▶ log estruturado → collector → Loki
└────────────────────┘
                    ┌────────────────────────────────┐
                    │ Grafana 13.2.6 → dashboard     │
                    │ uid=jornada-correlacionada     │
                    └────────────────────────────────┘
```

| Serviço | Imagem (pinada) | Porta (só loopback) | Papel |
|---|---|---|---|
| `api-gateway` | `python:3.12-slim` | `127.0.0.1:8080` | borda instrumentada: `POST /pagamentos`, `GET /health` |
| `fronteira-transacional` | `python:3.12-slim` | `127.0.0.1:8090` | simulador legado: `POST /transacao`, `GET /metricos` (alias `/metrics`) |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.165.0` | `4317/4318/8889/13133` | recebe, rotula, promove atributos, roteia |
| `prometheus` | `prom/prometheus:v3.16.0` | `127.0.0.1:9090` | séries temporais |
| `loki` | `grafana/loki:3.5.1` | `127.0.0.1:3100` | logs indexados por `correlation_id` |
| `jaeger` | `jaegertracing/all-in-one:1.76.0` | `127.0.0.1:16686` | traços |
| `grafana` | `grafana/grafana:13.2.6` | `127.0.0.1:3000` | dashboard + exploração |

Todas as portas são publicadas em `127.0.0.1`. Um lab que escuta em `0.0.0.0` numa máquina com
`docker0` é um achado de pentest esperando para acontecer.

## A chave de correlação: três mecanismos, cada um com dono

| Mecanismo | Campo | Quem escreve | Quem consome | Limite honesto |
|---|---|---|---|---|
| Contexto de traço | `traceparent` | SDK do gateway | collector → Jaeger | não sobrevive à fronteira |
| Contexto distribuído de negócio | `baggage: sre.correlation_id,sre.jornada,sre.estagio` | gateway | collector (transform), fronteira, logs | cabeçalho pode ser cortado por proxy |
| ID de transação | `X-Correlation-Id` | gateway | fronteira devolve no corpo (`corrid`) e em header | formato não bate — daí a normalização |

A decisão de usar **baggage W3C** em vez de inventar um header proprietário está escrita em
[ADR-001](docs/decisoes/ADR-001-baggage-vs-header-proprietario.md), com contexto, consequências e o
gatilho para reavaliar.

### Normalização de ID definida nas duas pontas

O sistema transacional devolve o identificador **maiúsculo e sem pontuação** (campo de largura fixa).
Forma canônica: minúsculas, sem hífens, 32 hex quando for UUID. A função é idempotente e existe
literalmente duas vezes no repo — uma por lado — porque comparação entre pontas só funciona se a
função for a mesma.

```python
normalizar_id("3F1A9C2E-77B0-4C1A-9E2D-1B0A7F6C5D4E")  # → "3f1a9c2e77b04c1a9e2d1b0a7f6c5d4e"
```

O simulador aceita quatro posturas via `--formato-id`: `maiusculo-sem-hifen`, `minusculo-sem-hifen`,
`com-hifen-truncado`, `ausente`. Trocar o valor e reobservar é o experimento mais instrutivo do lab.

Uma honestidade que o experimento ensina: com a borda **canonicizando antes de enviar** (32 hex sem
pontuação), a postura `com-hifen-truncado` deixa de doer — truncar 32 chars de uma string que já tem 32
chars é identidade. O corte de campo só vira incidente quando alguém envia o UUID com hífens (36 chars)
sem normalizar. A normalização na borda é justamente o conserto; ver `motivo=ok` para essa postura é o
resultado esperado, não o teste passando por acaso.

## A fronteira é auditada, não presumida

Onde a maioria das instrumentações para ("enviei o header, logo funcionou"), este lab **mede o
veredito da travessia** comparando o que foi enviado com o que voltou — header, corpo JSON (`corrid`)
ou resgate por regex quando o ID veio solto num texto:

| `motivo` | O que significa | Ação de SRE |
|---|---|---|
| `ok` | ID voltou idêntico após normalização | — |
| `resgatado-de-texto` | o ID só existia dentro de mensagem textual | contrato de resposta frágil; cobrar campo dedicado |
| `id-diferente(...)` | voltou outro ID | bug de propagação ou de reuso de buffer no legado |
| `id-ausente-na-resposta` | não voltou nada | jornada órfã: o painel de cobertura sobe |

Cobertura de correlação, a métrica que sustenta a conversa com o time do sistema transacional:

```promql
100 * sum(rate(sre_jornada_duration_seconds_count{etapa="chamada-fronteira",fronteira_propagou="true"}[10m]))
    / clamp_min(sum(rate(sre_jornada_duration_seconds_count{etapa="chamada-fronteira"}[10m])), 1)
```

## Vocabulário de telemetria (o contrato)

Nomes no SDK → nomes no Prometheus (o exporter converte o ponto em `_` e acrescenta a unidade):

| Métrica (SDK) | Série Prometheus | Tipo | O que responde |
|---|---|---|---|
| `sre.jornada.duration` (s) | `sre_jornada_duration_seconds` | histogram | onde a jornada gastou tempo, por `etapa`/`camada`/`resultado` |
| `sre.fronteira.relogio_skew` (s) | `sre_fronteira_relogio_skew_seconds` | histogram | de quanto o relógio da fronteira divergiu (estulado pelo header `X-Legado-Timestamp`) |
| — (fronteira, texto puro) | `fronteira_requisicoes_total` | counter | volume recebido pelo sistema transacional |
| — | `fronteira_correlacao_total{situacao="com-id\|sem-id"}` | counter | quantas travessias chegaram com a chave de negócio |
| — | `fronteira_rc_erro_total` | counter | retornos de erro (RC) do legado |

Dimensões fechadas por vocabulário (validadas pelo script):
`jornada` ∈ {pagamento-instantaneo, liquidacao-lote, consulta-portal} ·
`etapa` ∈ {recebeu-cliente, orquestrou, chamada-fronteira, processou-legado, respondeu} ·
`camada` ∈ {api, orquestração, fila, legado} → `api|orquestracao|fila|legado` ·
`resultado` ∈ {ok, erro, timeout, sem-correlacao} · `fronteira_propagou` ∈ {true, false, desconhecido}.

### Por que esses buckets

```
duração: 0.05 0.1 0.25 0.4 0.65 1.0 1.5 3.0 5.0 10.0 30.0   # segundos
skew:    0 0.05 0.1 0.25 0.5 1.0 2.0 5.0
```

Objetivos deste lab: **250 ms** por etapa de aplicação, **650 ms** de jornada completa (p95) e
**3 s** como teto absoluto antes de tratar como incidente. Bucket que não encosta no objetivo faz o
`histogram_quantile` interpolar num vão largo demais — o número na tela vira chute com duas casas
decimais. Escolhe-se o bucket a partir do SLO, nunca o contrário.

## Rodar

```bash
docker compose up -d
docker compose ps                      # espere healthy

# uma jornada completa: a chave de negócio vem do cliente, a jornada vem do baggage
curl -sS -X POST localhost:8080/pagamentos \
  -H 'content-type: application/json' \
  -H 'X-Correlation-Id: 3f1a9c2e-77b0-4c1a-9e2d-1b0a7f6c5d4e' \
  -H 'baggage: sre.jornada=pagamento-instantaneo,sre.estagio=autorizacao' \
  -d '{"valor_centavos":4990,"metodo":"pix"}'

# sem chave nenhuma: a borda gera, `origem_correlation_id` = "gerado-na-borda"
curl -sS -X POST 'localhost:8080/pagamentos?jornada=liquidacao-lote' \
  -H 'content-type: application/json' -d '{"valor_centavos":15000,"metodo":"pix"}'

curl -sS localhost:8080/health         # mostra telemetria ativa e URL da fronteira
open http://localhost:3000/d/jornada-correlacionada
```

Gerar carga com posturas hostis da fronteira e ver a cobertura cair:

```bash
# edite docker-compose.yml: --formato-id ausente  (ou com-hifen-truncado)
docker compose up -d --force-recreate fronteira-transacional
```

Juntar as três pernas no Loki — a consulta que o dashboard usa para drill-down:

```logql
{service_name=~"api-gateway|fronteira-transacional"} |~ "$correlation_id"
```

## Como este lab foi verificado (22/nov/2026)

Não como promessa de README: como saída de comando. O `scripts/fumaca.sh` sobe fronteira + gateway +
um receptor OTLP de vinte linhas, gera tráfego real e **exige** que os nomes das séries apareçam nos
payloads protobuf que saem pelo `OTLPMetricExporter`:

```
## /health
 telemetria_disponivel = True | otlp configurado
## travessias (veredito auditado pela borda)
 pagamento-instantaneo  propagou=True motivo=ok                     skew=-418.5ms
 liquidacao-lote        propagou=True motivo=ok                     skew=-418.8ms
 consulta-portal        propagou=True motivo=ok                     skew=-419.0ms
 pagamento-instantaneo  aceito=False origem_id=cliente
## scrape direto do texto Prometheus da fronteira (sem OTel)
 fronteira_requisicoes_total 4
 fronteira_correlacao_total{situacao="com-id"} 4
 fronteira_correlacao_total{situacao="sem-id"} 0
 fronteira_rc_erro_total 1
## o que chegou no receptor OTLP
  metrics    7013 bytes
  traces     5669 bytes
  logs       4846 bytes
  sre.jornada.duration         1 ocorrência(s)
  sre.fronteira.relogio_skew   1 ocorrência(s)
  fronteira_propagou           9 ocorrência(s)
RESULTADO: fumaça OK — as séries saíram por OTLP
```

O skew medido foi **-418 ms** com `--skew-ms 420` configurado: a diferença de 2 ms é o tempo de
viagem da chamada. É a prova de que o sinal importa (fronteira adiantada) e de que a estimativa
funciona — o histograma registra a **magnitude**, 418 ms, que é o que sustenta alerta.

**A parte que vale o texto: a primeira versão deste lab não exportava nada.** Três argumentos do SDK
foram escritos com nomes que não existem mais — `PeriodicMetricReader` (o certo é
`PeriodicExportingMetricReader`), `interval_millis` (`export_interval_millis`) e
`explicit_bucket_boundaries_advice` (`explicit_bucket_boundaries_advisory`). Os dois primeiros estouravam dentro do bloco `try` de import, caíam na degradação explícita e o serviço subia **aparentemente perfeito**
com `telemetria_disponivel=False`. O dashboard continuaria referenciando séries que nunca existiram, e o
`validate.py` não pegaria isso: ele confere o vocabulário contra o **código**, não contra o backend.

Duas lições que viraram mecanismo neste repo:

1. degradação precisa ser **visível e testada** — daí o `scripts/fumaca.sh` existir e falhar com
   exit code 1 se um nome de série não aparecer no payload;
2. validador de dashboard é necessário, mas **não é suficiente**: ele prova coerência estática; só o
   dado trafegado prova o desenho.

## Rodar sem Docker

```bash
python3 -m pip install -r requirements.txt
bash scripts/fumaca.sh                    # fumaça de ponta a ponta, tudo em loopback
# ou manualmente:
python3 src/fronteira_transacional.py --porta 8090 --skew-ms 420 --demora-base-ms 180 &
FRONTEIRA_URL=http://127.0.0.1:8090/transacao \
  python3 -m uvicorn --app-dir src instrumentacao_fastapi:app --port 8080
```

`--bind` do simulador vale `127.0.0.1` por padrão: num posto de trabalho não há motivo para um
servidor de teste escutar na rede. Dentro do contêiner o compose passa `--bind 0.0.0.0`, onde o
isolamento já é dado pelo publish `127.0.0.1:8090:8090`.

## Validar o dashboard sem subir nada

`dashboards/validate.py` (stdlib apenas, offline, ~200 ms) confere o JSON do Grafana contra o código e
contra a config do collector: estrutura e `schemaVersion`, datasources, `panelId` de targets aninhadas,
vocabulário de métricas/labels e — a parte que pega erro de verdade — se **toda** métrica citada em
PromQL existe em `src/` ou é exportada pelo collector.

```bash
python3 dashboards/validate.py                  # 0 erros, 0 avisos
python3 dashboards/validate.py --strict --json  # aviso vira falha, saída mecânica (CI)
python3 dashboards/validate.py --listar-metricas
```

Saída atual:

```
[ok  ] dashboards/jornada-correlacionada.json  uid=jornada-correlacionada  schemaVersion=41
        painéis=11 (consulta=10)
        variáveis: DS_LOKI, DS_PROM, correlation_id, etapa, jornada
        métricas:  fronteira_correlacao, sre_fronteira_relogio_skew_seconds, sre_jornada_duration_seconds
        labels:    camada, etapa, fronteira_propagou, jornada, service_name, situacao
1 dashboard(s) validado(s): 0 erro(s), 0 aviso(s)
```

## O collector é onde a mágica honesta acontece

Pipelines: `traces → otlp/jaeger`, `metrics → prometheus`, `logs → otlphttp/loki`, todos passando por
`memory_limiter → resource/labelling → transform/correlacao → batch`. O processor de transform promove
a chave que veio no baggage para atributo de primeiro plano:

```yaml
transform/correlacao:
  log_statements:
    - context: log
      statements:
        - set(attributes["correlation_id"], attributes["sre.correlation_id"]) where attributes["correlation_id"] == nil
        - set(attributes["jornada"],        attributes["sre.jornada"])        where attributes["jornada"] == nil
  span_statements:
    - context: span
      statements:
        - set(attributes["correlation.id"], attributes["sre.correlation_id"]) where attributes["correlation.id"] == nil
```

No config há um bloco comentado sobre amostragem: *head sampling decide sem saber se deu erro e descarta
justamente a cauda* — em produção eu usaria `tail_sampling` (decisão no fim do traço, com status e
latência em mãos), pagando buffer de memória e latência de decisão. Num lab o objetivo é ver tudo.

## O que este repositório **não** é

- Não é arquitetura de referência de nenhuma instituição: a fronteira é um `http.server` de stdlib que
  **imita** comportamentos incômodos (relógio deslocado, ID em caixa alta, RC de erro).
- Não mede latência de mainframe real nem substitui APM de vendor.
- Não contém dado de cliente, hostname, IP interno ou credencial — e `.env` é bloqueado por hook de
  pre-commit no repo pai deste kit.
- O serviço transacional exporta métricas em texto Prometheus na mão, sem SDK, de propósito: é o que
  um sistema que você não pode modificar faria.

## Estrutura

```
docker-compose.yml                       # 7 serviços, imagens pinadas, portas em 127.0.0.1
src/instrumentacao_fastapi.py            # borda: propagação, veredito de travessia, histogramas
src/fronteira_transacional.py            # legado simulado: skew, formatos de ID, /metricos próprio
scripts/fumaca.sh                        # teste de fumaça sem Docker (receptor OTLP incluso)
requirements.txt                         # versões exatas em que o lab foi executado
config/otel-collector-config.yaml        # receivers/processors/exporters + transform de correlação
config/prometheus.yml  config/loki-config.yaml
dashboards/jornada-correlacionada.json   # 11 painéis, 5 variáveis
dashboards/validate.py                   # validador offline (CI-ready)
docs/decisoes/ADR-001-*.md               # baggage vs header proprietário
LICENSE                                  # MIT
```

## English

Instrumentation pattern for correlating a business journey across a modern edge (FastAPI + OpenTelemetry)
and a transactional system that cannot be modified. Three propagation mechanisms with explicit ownership
(`traceparent`, W3C `baggage`, `X-Correlation-Id`), ID normalization defined identically on both ends, and —
the part most setups skip — an **audited boundary verdict**: the edge compares the ID it sent with the one it
got back (header, JSON body, or regex rescue from free text) and emits it as a label, so correlation coverage
becomes a time series instead of an assumption. `sre_jornada_duration_seconds` and
`sre_fronteira_relogio_skew_seconds` histograms carry bucket boundaries chosen from the lab SLOs (250 ms /
650 ms / 3 s). A collector `transform` processor promotes the business key to a first-class attribute, which
is what lets one LogQL query join traces, metrics and logs across both services. The Grafana dashboard JSON is
checked offline by a stdlib validator (`dashboards/validate.py`) that verifies every referenced metric actually
exists in `src/` or is exported by the collector — panels that reference phantom metrics fail in CI, not during
an incident. All seven containers bind to `127.0.0.1`; there are no credentials, hosts, clients or production
data anywhere in the repository.
