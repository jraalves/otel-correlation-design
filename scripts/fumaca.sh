#!/usr/bin/env bash
# fumaca.sh — teste de fumaça sem Docker: sobe a fronteira, um receptor OTLP de 20
# linhas e o gateway; gera tráfego; exige que as séries tenham saído por OTLP.
#
#   bash scripts/fumaca.sh
#
# Existe porque as três assinaturas do SDK OpenTelemetry usadas aqui mudaram de nome
# entre versões (PeriodicMetricReader, interval_millis, explicit_bucket_boundaries_
# advice) e, com o guard-rail de degradação, o serviço sobe PERFEITO sem exportar
# nada. Sem este script, o lab mente silenciosamente — que foi exatamente o que
# aconteceu na primeira versão dele.
set -u
cd "$(dirname "$0")/.."
RAIZ=$PWD
TMP=$(mktemp -d)
PORT_OTLP=${PORT_OTLP:-4328}
PORT_FR=${PORT_FR:-8691}
PORT_GW=${PORT_GW:-8680}

# shellcheck disable=SC2064
trap "kill \${GW:-} \${FR:-} \${STUB:-} 2>/dev/null; rm -rf $TMP" EXIT

python3 - "$PORT_OTLP" "$TMP" <<'PY' &
import sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
porta, pasta = int(sys.argv[1]), sys.argv[2]
class H(BaseHTTPRequestHandler):
    lock = threading.Lock()
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        corpo = self.rfile.read(n)
        tipo = self.path.rstrip("/").split("/")[-1]
        with self.lock, open(f"{pasta}/otlp_{tipo}.bin", "ab") as f:
            f.write(corpo)
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", porta), H).serve_forever()
PY
STUB=$!
sleep 0.7

COLETOR_LOGS_URL="http://127.0.0.1:$PORT_OTLP/v1/logs" \
  python3 src/fronteira_transacional.py --bind 127.0.0.1 --porta "$PORT_FR" \
    --skew-ms 420 --demora-base-ms 40 --formato-id maiusculo-sem-hifen >"$TMP/fr.log" 2>&1 &
FR=$!
sleep 0.7

OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:$PORT_OTLP" OTEL_SERVICE_NAME=api-gateway \
FRONTEIRA_URL="http://127.0.0.1:$PORT_FR/transacao" AMBIENTE=lab \
  python3 -m uvicorn --app-dir src instrumentacao_fastapi:app \
    --host 127.0.0.1 --port "$PORT_GW" --log-level warning >"$TMP/gw.log" 2>&1 &
GW=$!

for _ in $(seq 1 50); do curl -sf "localhost:$PORT_GW/health" >/dev/null && break; sleep 0.4; done

echo "## /health"
curl -sS "localhost:$PORT_GW/health" | python3 -c "import json,sys;d=json.load(sys.stdin);print(' telemetria_disponivel =',d['telemetria_disponivel'],'|',d.get('motivo_telemetria'))"

echo "## travessias (veredito auditado pela borda)"
for j in pagamento-instantaneo liquidacao-lote consulta-portal; do
  curl -sS -X POST "localhost:$PORT_GW/pagamentos" -H 'content-type: application/json' \
    -H "baggage: sre.jornada=$j,sre.estagio=autorizacao" -d '{"valor_centavos":4990,"metodo":"pix"}' \
  | python3 -c "import json,sys;d=json.load(sys.stdin);f=d['fronteira'];print(f\" {d['jornada']:22} propagou={f['propagou_id']} motivo={f['motivo']:22} skew={f['skew_relogio_ms']}ms\")"
done
curl -sS -X POST "localhost:$PORT_GW/pagamentos" -H 'content-type: application/json' \
  -H 'X-Correlation-Id: 9f1c2b3a-4d5e-4f60-a1b2-c3d4e5f60718' -d '{"modo":"erro","valor_centavos":100}' \
| python3 -c "import json,sys;d=json.load(sys.stdin);print(f\" {d['jornada']:22} aceito={d['aceito']} origem_id={d['origem_correlation_id']}\")"

echo "## scrape direto do texto Prometheus da fronteira (sem OTel)"
curl -sS "localhost:$PORT_FR/metricos" | grep -E '^fronteira_' | sed 's/^/ /'

echo "## aguardando o export periódico (10 s)"
sleep 13

echo "## o que chegou no receptor OTLP"
for tipo in metrics traces logs; do
  arq="$TMP/otlp_$tipo.bin"
  if [[ -f "$arq" ]]; then printf "  %-8s %6s bytes\n" "$tipo" "$(wc -c <"$arq")"; else printf "  %-8s AUSENTE\n" "$tipo"; fi
done
falha=0
for m in sre.jornada.duration sre.fronteira.relogio_skew fronteira_propagou jornada; do
  n=$(strings -a "$TMP"/otlp_*.bin 2>/dev/null | grep -c -- "$m")
  printf "  %-28s %s ocorrência(s)\n" "$m" "$n"
  [[ "$n" -gt 0 ]] || { echo "  FALHA: '$m' não apareceu em payload nenhum"; falha=1; }
done
grep -q 'CORRID=' "$TMP/fr.log" || { echo "  FALHA: fronteira não logrou no formato de relatório"; falha=1; }
[[ $falha -eq 0 ]] && echo "RESULTADO: fumaça OK — as séries saíram por OTLP" || { echo "RESULTADO: FALHOU"; exit 1; }
