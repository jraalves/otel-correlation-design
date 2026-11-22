#!/usr/bin/env python3
"""instrumentacao_fastapi.py — middleware de propagação de contexto na borda distribuída.

Implementação de referência própria, reconstruída a partir de prática. Não é
código de nenhum empregador ou cliente: nomes de serviço, jornada e métricas são
de exemplo, e não há endpoint, hostname ou credencial real aqui.

O que este arquivo demonstra, e por que ele existe:

1. **Duas chaves, dois papéis.** `traceparent` (W3C Trace Context) amarra spans e
   só existe onde há tracing. `sre.correlation_id` (W3C Baggage) amarra a
   *jornada de negócio* e é a única chave que sobrevive à travessia para um
   sistema transacional que não fala OTel. Perder a primeira é ruim; perder a
   segunda é ficar cego.

2. **A borda decide, o meio obedece.** O `correlation_id` nasce (ou é aceito)
   uma única vez, na entrada. Nenhum componente de meio de jornada gera ID novo:
   geração dispersa é o que produz "duas metades da mesma transação" com IDs
   diferentes e inauditáveis.

3. **Cardinalidade.** `correlation_id` vai para *span attribute* e para *log*.
   Ele **nunca** vai para atributo de métrica: com 10⁶ transações/dia você cria
   10⁶ séries. O que vira atributo de métrica é o que tem vocabulário fechado
   (`jornada`, `etapa`, `camada`, `resultado`, `fronteira_propagou`).

4. **Instrumentar a fronteira, não só o cliente.** A métrica
   `sre.jornada.duration` é emitida em cada etapa *com o veredito da travessia*
   (`fronteira_propagou=true|false`). Sem esse booleano, a perda de correlação é
   invisível até alguém tentar abrir um chamado e não achar o rastro.

Execução (o lab sobe isto via docker-compose; ver README, "Teste de fumaça"):
    pip install "fastapi>=0.115" "uvicorn>=0.30" "httpx>=0.27" \\
        "opentelemetry-api>=1.27" "opentelemetry-sdk>=1.27" "opentelemetry-exporter-otlp>=1.27"
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \\
        uvicorn instrumentacao_fastapi:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx

# --------------------------------------------------------------------------- #
# Vocabulário fechado (é isto que pode virar atributo de métrica)
# --------------------------------------------------------------------------- #

NOME_SERVICO = os.environ.get("OTEL_SERVICE_NAME", "api-gateway")
JORNADAS = {"pagamento-instantaneo", "liquidacao-lote", "consulta-portal"}
ETAPAS = {"recebeu-cliente", "orquestrou", "chamada-fronteira", "processou-legado", "respondeu"}
CAMADAS = {"api", "orquestracao", "fila", "legado"}
RESULTADOS = {"ok", "erro", "timeout", "sem-correlacao"}

HEADER_CORRELATION = "x-correlation-id"
BAGGAGE_CORRELATION = "sre.correlation_id"     # chave única de negócio no caminho todo
BAGGAGE_JORNADA = "sre.jornada"
BAGGAGE_ESTAGIO = "sre.estagio"

# 8 hex ou 32 hex sem hífen: o ID do legado chega sem pontuação. Aceitar só um
# formato na borda faz o retrabalho aparecer três sistemas adiante.
_ID_SOLTO = re.compile(r"\b[0-9a-f]{8}(?:-?[0-9a-f]{4}){3}-?[0-9a-f]{12}\b|\b[0-9a-f]{32}\b", re.I)


def normalizar_id(valor: str | None) -> str | None:
    """Forma canônica: minúsculas, sem hífens, 32 hex quando for UUID.

    A normalização precisa ser **idempotente e definida nas duas pontas**: o
    sistema transacional devolve o ID em maiúsculas e sem pontuação (campo de
    largura fixa), e quem compara depois tem que chegar no mesmo bytes.
    """
    if not valor:
        return None
    limpo = valor.strip().lower().replace("-", "")
    return limpo or None


def extrair_id_de_texto(texto: str | None) -> str | None:
    """Última linha de defesa: o ID dentro do corpo/log de um sistema que só tem
    texto. Usar isto é sinal de instrumentação incompleta, então o resultado é
    marcado como `resgatado-de-texto` e medido à parte."""
    if not texto:
        return None
    achado = _ID_SOLTO.search(texto)
    return normalizar_id(achado.group(0)) if achado else None


# --------------------------------------------------------------------------- #
# Telemetria
# --------------------------------------------------------------------------- #

loga = logging.getLogger("jornada")


@dataclass
class Telemetria:
    """Carrega tracer/meter com degradação explícita.

    Se o SDK OpenTelemetry não estiver instalado, o serviço continua atendendo e
    `disponivel=False` aparece no /health e nos logs. Um lab que quebra o serviço
    quando a observabilidade falta ensina a lição errada.
    """

    disponivel: bool = False
    tracer: object | None = None
    medidor: object | None = None
    propagador: object | None = None
    histograma_duracao: object = field(default=None, repr=False)
    histograma_skew: object = field(default=None, repr=False)
    erro_import: str = ""

    def registrar_duracao(self, *, jornada: str, etapa: str, camada: str,
                          segundos: float, fronteira_propagou: bool | None, resultado: str) -> None:
        if not self.disponivel or self.histograma_duracao is None:
            return
        atributos = {
            "jornada": jornada,
            "etapa": etapa,
            "camada": camada,
            "resultado": resultado,
            "fronteira_propagou": ("desconhecido" if fronteira_propagou is None
                                   else "true" if fronteira_propagou else "false"),
        }
        # nome no SDK  ->  nome no Prometheus (o exporter converte `sre.jornada.duration`
        # + unit "s" em `sre_jornada_duration_seconds`). Dashboard e alerta usam o segundo.
        self.histograma_duracao.record(max(segundos, 0.0), atributos)

    def registrar_skew(self, *, fronteira: str, jornada: str, segundos: float) -> None:
        if not self.disponivel or self.histograma_skew is None:
            return
        # O histograma registra a MAGNITUDE da divergência: o que sustenta alerta e
        # correlação de eventos é "de quanto os relógios diferem". O sinal (quem está
        # adiantado) continua no corpo da resposta, em `fronteira.skew_relogio_ms`, onde
        # é legível por humano. Bucket negativo não existe: com sinal, tudo cairia no
        # primeiro bucket e a série contaria mentira.
        self.histograma_skew.record(abs(segundos), {"fronteira": fronteira, "jornada": jornada})


telemetria = Telemetria()


def configurar_telemetria() -> Telemetria:
    global telemetria
    try:
        from opentelemetry import baggage, metrics, trace
        from opentelemetry.baggage.propagation import W3CBaggagePropagator
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    except Exception as exc:  # ImportError ou configuração ausente
        telemetria = Telemetria(disponivel=False, erro_import=f"{type(exc).__name__}: {exc}")
        return telemetria

    # A criação de instrumentos entra no mesmo guard-rail que os imports: assinatura
    # de SDK muda (o nome dos argumentos já mudou mais de uma vez), e um lab que morre no boot
    # por causa disso ensina a lição errada. /health passa a mostrar o motivo.
    try:
        recurso = Resource.create({
            "service.name": NOME_SERVICO,
            "service.namespace": "jornada-pagamento",
            "deployment.environment.name": os.environ.get("AMBIENTE", "lab"),
            "cloud.region": os.environ.get("REGIAO", "br-sp-1"),
        })
        provedor_tracos = TracerProvider(resource=recurso)
        provedor_tracos.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provedor_tracos)

        provedor_metricas = MeterProvider(
            resource=recurso,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000)],
        )
        metrics.set_meter_provider(provedor_metricas)

        # W3C primeiro: é o formato que o resto do caminho (e o Collector) entende.
        # Header proprietário de correlação continua sendo lido no middleware, mas não
        # é registrado como propagador global - ver ADR-001.
        set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

        medidor = metrics.get_meter("jornada.v1")
        telemetria = Telemetria(
            disponivel=True,
            tracer=trace.get_tracer("jornada.v1"),
            medidor=medidor,
            histograma_duracao=medidor.create_histogram(
                name="sre.jornada.duration",
                unit="s",
                description="Duração de cada etapa de uma jornada, com veredito da travessia de fronteira.",
                #_buckets escolhidos para cobrir os objetivos declarados no README
                # (250ms/650ms/3s). Bucket não cobre objetivo = quantil é chute.
                explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.4, 0.65, 1.0, 1.5, 3.0, 5.0, 10.0, 30.0],
            ),
            histograma_skew=medidor.create_histogram(
                name="sre.fronteira.relogio_skew",
                unit="s",
                description="Diferença de relógio estimada entre a borda distribuída e o sistema transacional.",
                explicit_bucket_boundaries_advisory=[0.0, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
            ),
        )
    except Exception as exc:
        telemetria = Telemetria(disponivel=False, erro_import=f"{type(exc).__name__}: {exc}")
        return telemetria

    # `baggage` fica importado para deixar explícito de onde vêm as helpers usadas
    telemetria.__dict__.setdefault("modulo_baggage", baggage)
    return telemetria


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    configurar_telemetria()
    loga.info(json.dumps({"evento": "inicio", "servico": NOME_SERVICO,
                          "telemetria_disponivel": telemetria.disponivel,
                          "erro": telemetria.erro_import}, ensure_ascii=False))
    yield


app = FastAPI(title="api-gateway (lab de correlação)", lifespan=ciclo_de_vida)
_http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))


# --------------------------------------------------------------------------- #
# O middleware: decide a chave de jornada e propaga
# --------------------------------------------------------------------------- #

@dataclass
class ContextoJornada:
    correlation_id: str
    origem_do_id: str          # cliente | baggage | gerado | texto  (audita a procedência)
    traceparent_recebido: bool
    jornada: str
    estagio: str
    contexto: dict = field(default_factory=dict)

    def como_log(self) -> dict:
        return {
            "correlation_id": self.correlation_id,
            "origem_correlation_id": self.origem_do_id,
            "traceparent_recebido": self.traceparent_recebido,
            "jornada": self.jornada,
            "estagio": self.estagio,
        }


async def resolver_contexto(request: Request) -> ContextoJornada:
    """Ordem de resolução do ID de negócio - e é isto que se revisa num PR."""
    from opentelemetry.propagate import extract

    contexto = extract(carrier=dict(request.headers))
    cabecalho = normalizar_id(request.headers.get(HEADER_CORRELATION))

    baggage_bruto = request.headers.get("baggage", "")
    no_baggage = normalizar_id(_valor_do_baggage(baggage_bruto, BAGGAGE_CORRELATION))

    if cabecalho:
        valor, origem = cabecalho, "cliente"
    elif no_baggage:
        valor, origem = no_baggage, "baggage"
    else:
        valor, origem = normalizar_id(uuid.uuid4().hex), "gerado-na-borda"

    jornada = _valor_do_baggage(baggage_bruto, BAGGAGE_JORNADA) or request.query_params.get("jornada") or "pagamento-instantaneo"
    if jornada not in JORNADAS:
        jornada = "pagamento-instantaneo"  # vocabulário fechado: nunca texto livre em label
    estagio = _valor_do_baggage(baggage_bruto, BAGGAGE_ESTAGIO) or "autorizacao"
    return ContextoJornada(valor, origem, "traceparent" in request.headers, jornada, estagio, contexto)


def _valor_do_baggage(header: str, chave: str) -> str | None:
    for parte in header.split(","):
        k, _, v = parte.strip().partition("=")
        if k == chave:
            return v.strip() or None
    return None


def montar_baggage(ctx: ContextoJornada) -> str:
    """Baggage que sai para o próximo elo.

    Regra que evita incidente: baggage é **texto plano no header**. Nada de PAN,
    documento fiscal, nome de cliente ou valor de transação. Só chave de
    correlação e vocabulário de jornada - que são, eles mesmos, o que interessa.
    """
    return ",".join([
        f"{BAGGAGE_CORRELATION}={ctx.correlation_id}",
        f"{BAGGAGE_JORNADA}={ctx.jornada}",
        f"{BAGGAGE_ESTAGIO}={ctx.estagio}",
    ])


@app.middleware("http")
async def correlacionar(request: Request, call_next):
    inicio = time.perf_counter()
    ctx = await resolver_contexto(request)
    request.state.jornada = ctx

    atributos_span = {
        "correlation.id": ctx.correlation_id,
        "journey.name": ctx.jornada,
        "journey.stage": ctx.estagio,
        "journey.id_origin": ctx.origem_do_id,
    }
    tracer = telemetria.tracer
    span = None
    if tracer is not None:
        from opentelemetry.trace import Status, StatusCode  # noqa: F401 (uso no ramo de erro)

        with tracer.start_as_current_span(
            f"POST {request.url.path}", context=ctx.contexto, attributes=atributos_span
        ) as span_recebida:
            span = span_recebida
            resposta = await call_next(request)
            dur = time.perf_counter() - inicio
            span.set_attribute("http.response.status_code", resposta.status_code)
            telemetria.registrar_duracao(
                jornada=ctx.jornada, etapa="recebeu-cliente", camada="api",
                segundos=dur, fronteira_propagou=None,
                resultado="ok" if resposta.status_code < 500 else "erro",
            )
            resposta.headers[HEADER_CORRELATION] = ctx.correlation_id
            resposta.headers["X-Origem-Correlation-Id"] = ctx.origem_do_id
            _logar(request, ctx, dur, resposta.status_code)
            return resposta

    resposta = await call_next(request)
    dur = time.perf_counter() - inicio
    resposta.headers[HEADER_CORRELATION] = ctx.correlation_id
    _logar(request, ctx, dur, resposta.status_code)
    return resposta


def _logar(request: Request, ctx: ContextoJornada, dur: float, status: int) -> None:
    """Log estruturado com as duas chaves.

    `trace_id` é o link clicável para o trace; `correlation_id` é a chave com que
    se cruza com o mundo transacional. Um documento de incidente que cite só a
    primeira não sobrevive ao sampling.
    """
    trace_id = ""
    span_id = ""
    if telemetria.disponivel:
        try:
            from opentelemetry import trace as _t

            ctx_ativo = _t.get_current_span().get_span_context()
            trace_id = format(ctx_ativo.trace_id, "032x") if ctx_ativo.is_valid else ""
            span_id = format(ctx_ativo.span_id, "016x") if ctx_ativo.is_valid else ""
        except Exception:
            pass
    linha = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "nivel": "INFO" if status < 500 else "ERROR",
        "servico": NOME_SERVICO,
        "etapa": "recebeu-cliente",
        "camada": "api",
        "metodo": request.method,
        "rota": request.url.path,
        "status": status,
        "duracao_ms": round(dur * 1000, 1),
        "trace_id": trace_id,
        "span_id": span_id,
        **ctx.como_log(),
    }
    loga.info(json.dumps(linha, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Rota que atravessa a fronteira
# --------------------------------------------------------------------------- #

@dataclass
class VereditoFronteira:
    propagou: bool
    motivo: str
    duracao_legado_s: float | None
    skew_s: float | None
    texto_resgatado: bool


def _avaliar_travessia(resposta: httpx.Response, ctx: ContextoJornada) -> VereditoFronteira:
    """Onde a correlação é *auditada*, não só suposta.

    O sistema transacional devolve `CORRID` no corpo e `X-Legado-Duracao-Ms` /
    `X-Legado-Timestamp` em headers de saída. Comparar o que voltou com o que foi
    enviado é o que transforma "achamos que funciona" em série temporal.
    """
    voltou = normalizar_id(resposta.headers.get(HEADER_CORRELATION)) or normalizar_id(
        (resposta.json() or {}).get("corrid") if "application/json" in resposta.headers.get("content-type", "") else None
    )
    corpo_texto = resposta.text
    resgatado = False
    if not voltou:
        voltou = extrair_id_de_texto(corpo_texto)
        resgatado = bool(voltou)
    if not voltou:
        return VereditoFronteira(False, "id-ausente-na-resposta", None, None, resgatado)
    if voltou != ctx.correlation_id:
        return VereditoFronteira(False, f"id-diferente({voltou[:8]}...)", None, None, resgatado)

    dur_ms = resposta.headers.get("x-legado-duracao-ms")
    ts_legado = resposta.headers.get("x-legado-timestamp")
    skew = None
    if ts_legado:
        try:
            # Diferença entre o relógio daqui e o carimbo que a fronteira pôs na resposta.
            # Negativo = relógio da fronteira adiantado. `dur_ms` é outra grandeza e entra
            # em `duracao_legado_s`; misturar as duas aqui seria erro de 180 ms constante.
            skew = time.time() - float(ts_legado)
        except ValueError:
            skew = None
    return VereditoFronteira(
        propagou=True,
        motivo="resgatado-de-texto" if resgatado else "ok",
        duracao_legado_s=(float(dur_ms) / 1000.0 if dur_ms else None),
        skew_s=skew,
        texto_resgatado=resgatado,
    )


@app.post("/pagamentos")
async def criar_pagamento(request: Request) -> JSONResponse:
    ctx: ContextoJornada = request.state.jornada
    corpo = await request.json()
    url_fronteira = os.environ.get("FRONTEIRA_URL", "http://fronteira-transacional:8090/transacao")

    inicio = time.perf_counter()
    headers = {
        "baggage": montar_baggage(ctx),
        HEADER_CORRELATION: ctx.correlation_id,
        "X-Enviado-Em": f"{time.time():.6f}",
    }
    if telemetria.disponivel:
        from opentelemetry.propagate import inject

        inject(headers)  # escreve traceparent + baggage no request que sai

    try:
        resposta = await _http.post(url_fronteira, json=corpo, headers=headers)
        dur = time.perf_counter() - inicio
        veredito = _avaliar_travessia(resposta, ctx)
    except httpx.TimeoutException:
        dur = time.perf_counter() - inicio
        telemetria.registrar_duracao(jornada=ctx.jornada, etapa="chamada-fronteira", camada="fila",
                                     segundos=dur, fronteira_propagou=False, resultado="timeout")
        return JSONResponse({"erro": "timeout-na-fronteira", "correlation_id": ctx.correlation_id}, status_code=504)

    telemetria.registrar_duracao(
        jornada=ctx.jornada, etapa="chamada-fronteira", camada="fila", segundos=dur,
        fronteira_propagou=veredito.propagou,
        resultado="ok" if veredito.propagou else "sem-correlacao",
    )
    if veredito.duracao_legado_s is not None:
        telemetria.registrar_duracao(jornada=ctx.jornada, etapa="processou-legado", camada="legado",
                                     segundos=veredito.duracao_legado_s, fronteira_propagou=veredito.propagou,
                                     resultado="ok")
    if veredito.skew_s is not None:
        telemetria.registrar_skew(fronteira="sistema-transacional", jornada=ctx.jornada, segundos=veredito.skew_s)

    return JSONResponse({
        "aceito": resposta.status_code == 200,
        "correlation_id": ctx.correlation_id,
        "origem_correlation_id": ctx.origem_do_id,
        "jornada": ctx.jornada,
        "fronteira": {
            "propagou_id": veredito.propagou,
            "motivo": veredito.motivo,
            "duracao_legado_ms": None if veredito.duracao_legado_s is None else round(veredito.duracao_legado_s * 1000, 1),
            "skew_relogio_ms": None if veredito.skew_s is None else round(veredito.skew_s * 1000, 1),
        },
    })


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "servico": NOME_SERVICO,
        "telemetria_disponivel": telemetria.disponivel,
        "motivo_telemetria": telemetria.erro_import or "otlp configurado",
        "fronteira_url": os.environ.get("FRONTEIRA_URL", "http://fronteira-transacional:8090/transacao"),
        "jornadas_suportadas": sorted(JORNADAS),
    })


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORTA", "8080")))
