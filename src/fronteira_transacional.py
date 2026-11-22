#!/usr/bin/env python3
"""fronteira_transacional.py — simulador de fronteira sem tracing nativo.

Implementação de referência própria, reconstruída de prática. **Isto não é, nem
representa, código de nenhum sistema real**: é um processo Python stdlib que se
comporta como um sistema transacional do ponto de vista de telemetria, para que
qualquer pessoa consiga exercitar o desenho de correlação sem ter acesso a um
mainframe.

O que ele imita (e por que isso é o problema todo):

* **não fala OpenTelemetry.** Não há SDK, não há `traceparent` propagado para
  dentro, não há span. Só há texto: um cabeçalho de entrada e um log de saída;
* **tem relógio próprio e independente.** O `--skew-ms` simula a diferença entre
  o relógio da plataforma distribuída e o do sistema transacional (em ambiente
  real, NTP de LPAR e NTP de cluster não conversam entre si);
* **tem campo de largura fixa.** O ID de negócio chega num campo `PIC X(32)` que
  não aceita pontuação: UUID com hífens chega truncado ou reformulado. Este
  simulador reproduz as duas variantes com `--formato-id`;
* **loga em formato de relatório**, sem ano, sem milissegundo, sem fuso, em
  maiúsculas - igualzinho ao que se encontra do outro lado, o que obriga a ter
  um parser defensivo do lado de cá.

Pseudo-código do programa transacional correspondente (COBOL-like, comentado).
Não compile isto: é a explicação do que o header precisa atravessar.

    IDENTIFICATION DIVISION.
    PROGRAM-ID.  PAG001.
    ENVIRONMENT DIVISION.
    DATA DIVISION.
    WORKING-STORAGE SECTION.
    *> 32 bytes, sem pontuação. O formato do ID é decisão de contrato,
    *> não detalhe de implementação: se o lado distribuído usa UUID com
    *> hífens (36 chars), ele NÃO cabe aqui sem normalização.
    01  WS-CORRID           PIC X(32).
    01  WS-BAGGAGE          PIC X(256).
    01  WS-ENVIADO-EM       PIC S9(13)V9(6) COMP-3.
    01  WS-INICIO           PIC S9(15) COMP-3.
    01  WS-FIM              PIC S9(15) COMP-3.

    PROCEDURE DIVISION.
    0000-PRINCIPAL.
        EXEC CICS RECEIVE CHANNEL-IDATA
                COMMAREA(WS-COMMAREA)
                LENGTH(WS-LENG)
        END-EXEC.

        *> 1) O header de transporte (traceparent) morre aqui: o runtime não
        *>    tem propagação de contexto distribuído. Nada a fazer além de
        *>    registrar que esta fronteira existe (ver ADR-001).
        *>
        *> 2) O que sobrevive é a chave de negócio. Ela vem no cabeçalho
        *>    dedicado OU dentro do baggage propagado pelo elo anterior.
        MOVE FUNCTION UPPER-CASE(WS-CORR-IN) TO WS-CORRID.
        INSPECT WS-CORRID TALLYING ... FOR '-'   *> remove pontuação
        IF WS-CORRID = SPACES
            *> pior falha: segue sem chave e ninguém sabe. Aqui se marca
            *> no log para o parser do lado de cá contar órfãos.
            MOVE 'CORRID-NAO-INFORMADO' TO WS-CORRID
        END-IF.

        EXEC CICS ASKTIME ABSTIME(WS-INICIO) END-EXEC.

        PERFORM 1000-VALIDAR-TITULAR.
        PERFORM 2000-ENVIAR-INSTRUCAO.

        EXEC CICS ASKTIME ABSTIME(WS-FIM) END-EXEC.

        *> 3) O log é o único rastro. Tem que conter, no mínimo: a chave, a
        *>    etapa, o código de retorno e o tempo. Timestamp sem ano e sem
        *>    fuso é o que obriga a reconstruir data na consulta.
        STRING  'DFH SM ' FUNCTION DATE-CICS ' ' FUNCTION TIME
                ' PGM=PAG001' ' CORRID=' WS-CORRID
                ' DUR=' (WS-FIM - WS-INICIO) 'MS' ' RC=' WS-RC
                INTO WS-LINHA-LOG.
        EXEC CICS WRITE OPERATOR TEXT(WS-LINHA-LOG) END-EXEC.

        *> 4) Devolver a chave é o que permite auditar a travessia do lado de
        *>    cá sem parser de log. Barato, e a maior parte das fronteiras
        *>    simplesmente não faz.
        EXEC CICS SEND CHANNEL-IDATA
                COMMAREA(WS-COMMAREA-OUT)
        END-EXEC.
        EXEC CICS RETURN END-EXEC.

Uso local (sem Docker):
    python3 src/fronteira_transacional.py --porta 8090 --skew-ms 420 --formato-id maiusculo-sem-hifen
Depois:
    curl -s -XPOST localhost:8090/transacao -H 'X-Correlation-Id: 9f1c2b3a-4d5e-4f60-a1b2-c3d4e5f60718' \\
         -H 'baggage: sre.correlation_id=9f1c2b3a-4d5e-4f60-a1b2-c3d4e5f60718,sre.jornada=pagamento-instantaneo' \\
         -d '{"valor_centavos":15000,"metodo":"pix"}'
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORMATOS_ID = ("maiusculo-sem-hifen", "minusculo-sem-hifen", "com-hifen-truncado", "ausente")

CONTADOR = {
    "requisicoes": 0,
    "com_id": 0,
    "sem_id": 0,
    "rc_erro": 0,
    "por_modo": {},
}
CADEADO = threading.Lock()

_BAGGAGE_PAR = re.compile(r"([!#$%&'*+\-.^_`|~0-9A-Za-z]+)=([^,;]*)")


def interpretar_baggage(header: str | None) -> dict:
    """Parser mínimo de W3C Baggage (RFC 9110 / W3C).

    Não usar lib aqui é decisão: a fronteira precisa conseguir interpretar o
    baggage com o runtime que ela tem, e em muitos casos isso é 20 linhas de
    string. Se o parser falhar, ele devolve vazio e o chamador trata como ID
    ausente - nunca estoura exceção no caminho transacional.
    """
    resultado: dict[str, str] = {}
    if not header:
        return resultado
    for parte in header.split(","):
        par = _BAGGAGE_PAR.match(parte.strip())
        if par:
            resultado[par.group(1)] = par.group(2).strip()
    return resultado


def aplicar_formato_id(valor: str | None, formato: str) -> str | None:
    """É aqui que o ID muda de forma - e é por isso que a comparação final precisa
    ser feita na forma normalizada, não na forma original."""
    if not valor or formato == "ausente":
        return None
    limpo = valor.strip().lower().replace("-", "")
    if formato == "maiusculo-sem-hifen":
        return limpo.upper()
    # Sem acento: o valor vem de FORMATOS_ID/CLI, que é ASCII. Comparar com
    # "hífen" deixava este ramo morto e o comportamento certo caía no default.
    if formato == "minusculo-sem-hifen":
        return limpo
    if formato == "com-hifen-truncado":
        # campo PIC X(32) com UUID de 36 chars: corta o rabo. O ID volta
        # "parecido" e a comparação byte a byte falha sem ninguém perceber.
        return valor.strip()[:32]
    return limpo


class Manipulador(BaseHTTPRequestHandler):
    server_version = "FronteiraTransacional/1.0"
    formato_id = FORMATOS_ID[0]
    skew_ms = 0.0
    demora_base_ms = 120.0
    sorteio = random.Random(20260522)

    # log do servidor é substituído pelo nosso formato de relatório
    def log_message(self, formato: str, *args) -> None:  # noqa: A003 (assinatura da base)
        pass

    def _capturar(self) -> tuple[str | None, dict]:
        cabecalho = self.headers.get("X-Correlation-Id") or self.headers.get("x-correlation-id")
        baggage = interpretar_baggage(self.headers.get("baggage"))
        chave = cabecalho or baggage.get("sre.correlation_id")
        return chave, baggage

    def _enviar_para_coletor(self, campo: dict, linha: str) -> None:
        """Envia o registro de log ao Collector por OTLP/HTTP (stdlib, urllib).

        Este é o ponto do desenho que merece destaque: a fronteira não fala OTel,
        mas um *shim* de 25 linhas na borda dela coloca o log de relatório dentro
        da plataforma, com a chave de correlação como atributo. É assim que se
        traz sistema legado para consulta unificada sem tocar no programa
        transacional - e é muito mais barato do que instrumentar o outro lado.
        """
        url = os.environ.get("COLETOR_LOGS_URL")
        if not url:
            return
        nano = time.time_ns()
        atributos = [
            {"key": k, "value": {"stringValue": str(v)}}
            for k, v in {
                "correlation_id": campo.get("corrid_normalizado") or "",
                "etapa": campo.get("etapa", "processou-legado"),
                "camada": "legado",
                "jornada": campo.get("jornada", ""),
                "rc": campo.get("rc", "00"),
                "duracao_ms": campo.get("dur_ms", 0),
                "skew_ms": round(self.skew_ms, 1),
                "traceparent_recebido": bool(campo.get("traceparent")),
            }.items()
        ]
        payload = {
            "resourceLogs": [{
                "resource": {"attributes": [
                    {"key": "service_name", "value": {"stringValue": "fronteira-transacional"}},
                    {"key": "deployment_environment_name", "value": {"stringValue": "lab"}},
                    {"key": "service_namespace", "value": {"stringValue": "jornada-pagamento"}},
                ]},
                "scopeLogs": [{
                    "scope": {"name": "fronteira.shim", "version": "1.0"},
                    "logRecords": [{
                        "timeUnixNano": str(nano),
                        "observedTimeUnixNano": str(nano),
                        "severityNumber": 9 if campo.get("rc") == "00" else 17,
                        "severityText": "INFO" if campo.get("rc") == "00" else "ERROR",
                        "body": {"stringValue": linha},
                        "attributes": atributos,
                    }],
                }],
            }]
        }
        try:
            requisicao = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(requisicao, timeout=1.5) as resposta:
                resposta.read(64)
        except Exception as exc:  # o shim nunca pode derrubar a transação
            print(f"WARN shim-log-falhou {type(exc).__name__}: {exc}", flush=True)

    def _registrar_log(self, campo: dict) -> None:
        """Log no formato do outro mundo: maiúsculo, sem ano, sem fuso, espaço-separado.

        Este formato é deliberadamente hostil a parser. A lição do desenho é:
        a correlação tem que sobreviver a um log que não foi feito para ser
        consultado. `print` em stdout vai para o Loki via Collector.
        """
        agora = datetime.now()  # sem fuso de propósito
        linha = (
            f"DFH SM {agora:%m%d} {agora:%H%M%S} PGM=PAG001"
            f" CORRID={campo.get('corrid', 'CORRID-NAO-INFORMADO')}"
            f" ETAPA={campo.get('etapa', 'processou-legado')}"
            f" JORNADA={campo.get('jornada', '-INDEFINIDA-')}"
            f" DUR={campo.get('dur_ms', 0):07d}MS"
            f" RC={campo.get('rc', '00')}"
            f" TRACE={'AUSENTE' if campo.get('traceparent') is None else 'RECEBIDO-NAO-PROPAGADO'}"
        )
        print(linha, flush=True)
        self._enviar_para_coletor(campo, linha)

    def _responder(self, codigo: int, corpo: dict, headers: dict) -> None:
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        for chave, valor in headers.items():
            self.send_header(chave, valor)
        self.end_headers()
        self.wfile.write(dados)

    def do_POST(self) -> None:  # noqa: N802 (assinatura da base)
        caminho = self.path.split("?")[0]
        corpo_bruto = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        if caminho != "/transacao":
            self._responder(404, {"erro": "rota-inexistente",
                                  "rotas": ["/transacao", "/metricos", "/metrics"]}, {})
            return

        chave, baggage = self._capturar()
        modo = "ok"
        try:
            entrada = json.loads(corpo_bruto.decode("utf-8") or "{}")
            modo = str(entrada.get("modo", "ok"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            entrada, modo = {}, "corpo-invalido"

        inicio = time.perf_counter()
        demora_ms = self.sorteio.uniform(0.6, 1.4) * self.demora_base_ms
        if modo == "lento":
            demora_ms = self.demora_base_ms * self.sorteio.uniform(8, 20)
        time.sleep(min(demora_ms, 15_000) / 1000.0)
        dur_ms = (time.perf_counter() - inicio) * 1000.0

        corrid = aplicar_formato_id(chave, self.formato_id)
        normalizado = (corrid or "").lower().replace("-", "") or None
        rc = "00" if modo not in ("erro", "corpo-invalido") else "16"
        etapa = "processou-legado"

        self._registrar_log({
            "corrid": corrid or "CORRID-NAO-INFORMADO",
            "corrid_normalizado": normalizado,
            "etapa": etapa,
            "jornada": baggage.get("sre.jornada", ""),
            "dur_ms": int(dur_ms),
            "rc": rc,
            "traceparent": self.headers.get("traceparent"),
        })
        with CADEADO:
            CONTADOR["requisicoes"] += 1
            CONTADOR["com_id" if corrid else "sem_id"] += 1
            CONTADOR["rc_erro"] += 1 if rc != "00" else 0
            CONTADOR["por_modo"][modo] = CONTADOR["por_modo"].get(modo, 0) + 1

        headers = {
            "X-Legado-Duracao-Ms": f"{dur_ms:.1f}",
            # timestamp do relógio DA FRONTEIRA, deslocado pelo skew configurado:
            # é o que permite a quem chama estimar a diferença em vez de assumir 0
            "X-Legado-Timestamp": f"{time.time() + self.skew_ms / 1000.0:.6f}",
            "X-Relogio-Fronteira": "independente",
        }
        if corrid:
            headers["X-Correlation-Id"] = corrid
        corpo = {
            "aceito": rc == "00",
            "rc": rc,
            "corrid": corrid,
            "duracao_ms": round(dur_ms, 1),
            "modo": modo,
            "baggage_recebido": {k: v for k, v in baggage.items() if k.startswith("sre.")},
            "traceparent_recebido": bool(self.headers.get("traceparent")),
            "observacao": ("este processo não propaga contexto distribuído para dentro; "
                           "só a chave de negócio sobrevive"),
        }
        self._responder(200 if rc == "00" else 500, corpo, headers)

    def do_GET(self) -> None:  # noqa: N802
        # `/metricos` é o que o config/prometheus.yml raspa (o padrão do scrape
        # interno pode ser não padrão quando o outro lado é um sistema legado).
        # `/metrics` é aceito como alias, porque é o reflexo de quem chega aqui.
        if self.path.split("?")[0] not in ("/metricos", "/metrics"):
            self._responder(404, {"erro": "rota-inexistente", "rotas": ["/metricos", "/metrics"]}, {})
            return
        with CADEADO:
            copia = dict(CONTADOR)
            copia["por_modo"] = dict(copia["por_modo"])
        texto = "\n".join([
            "# HELP fronteira_requisicoes_total Requisições recebidas pela fronteira",
            "# TYPE fronteira_requisicoes_total counter",
            f"fronteira_requisicoes_total {copia['requisicoes']}",
            "# HELP fronteira_correlacao_total Travessias com e sem chave de negócio",
            "# TYPE fronteira_correlacao_total counter",
            f'fronteira_correlacao_total{{situacao="com-id"}} {copia["com_id"]}',
            f'fronteira_correlacao_total{{situacao="sem-id"}} {copia["sem_id"]}',
            "# HELP fronteira_rc_erro_total Retornos de erro do sistema transacional",
            "# TYPE fronteira_rc_erro_total counter",
            f"fronteira_rc_erro_total {copia['rc_erro']}",
            "",
        ])
        dados = texto.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)


def principal(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fronteira_transacional.py",
        description="Simulador stdlib de uma fronteira transacional sem tracing nativo. "
                    "Serve para exercitar correlação de jornada sem ter mainframe.",
    )
    parser.add_argument("--porta", type=int, default=8090)
    # Padrão loopback: um simulador numa estação de trabalho não tem motivo para
    # escutar na rede. Dentro do contêiner o compose passa `--bind 0.0.0.0`, e o
    # publish `127.0.0.1:8090:8090` continua limitando o acesso ao host.
    parser.add_argument("--bind", default="127.0.0.1",
                        help="interface de escuta (padrão loopback; 0.0.0.0 só dentro de contêiner)")
    parser.add_argument("--skew-ms", type=float, default=0.0,
                        help="diferença de relógio simulada, em ms (positivo = fronteira adiantada)")
    parser.add_argument("--demora-base-ms", type=float, default=120.0)
    parser.add_argument("--formato-id", choices=FORMATOS_ID, default=FORMATOS_ID[0],
                        help="como o campo fixo do sistema transacional trata o ID recebido")
    args = parser.parse_args(argv)

    Manipulador.formato_id = args.formato_id
    Manipulador.skew_ms = args.skew_ms
    Manipulador.demora_base_ms = args.demora_base_ms

    servidor = ThreadingHTTPServer((args.bind, args.porta), Manipulador)
    print(json.dumps({
        "evento": "fronteira-iniciada", "bind": args.bind, "porta": args.porta, "skew_ms": args.skew_ms,
        "formato_id": args.formato_id, "demora_base_ms": args.demora_base_ms,
        "aviso": "simulador: nenhuma linha deste arquivo veio de sistema real",
    }, ensure_ascii=False), flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(principal())
