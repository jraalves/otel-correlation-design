# ADR-001 — Baggage W3C para a chave de negócio, em vez de header proprietário de correlação

- **Status:** Aceito, com ressalva documentada (reavaliar quando o último elo sem OTel for removido)
- **Data:** 2026-05-14
- **Decisão de:** SRE / observabilidade (implementação de referência própria, reconstruída de prática — não é artefato de nenhum empregador ou cliente)
- **Contexto correlato:** [`../../README.md`](../../README.md), seções "Estratégia de correlação" e "Limitações"

## Contexto

Uma jornada de pagamento cruza elos que não falam a mesma língua de telemetria:
API em Kubernetes, orquestração, mensageria e um sistema transacional que não tem
SDK, não tem sampling, não propaga contexto distribuído e não vai ganhar isso tão
cedo. O `traceparent` da W3C morre nessa fronteira — literalmente: o runtime do
outro lado não tem onde guardar contexto de trace.

Sobrou desenhar a chave que sobrevive. As duas opções reais em qualquer estate
desses são:

1. **Header proprietário** de correlação (`X-Correlation-Id`, ou o nome que o
   padrão interno da empresa escolheu), propagado por convenção e por revisão de
   código;
2. **W3C Baggage** carregando a chave de negócio como par estruturado
   (`sre.correlation_id=...`), junto com os metadados de jornada
   (`sre.jornada`, `sre.estagio`).

A pergunta não é "qual é mais bonito", e sim: **o que acontece quando um elo
esquece de propagar?** No caso 1, o ID desaparece em silêncio e você descobre
numa auditoria, três meses depois, que 30% das jornadas não têm metade do rastro.
No caso 2, o ID desaparece em silêncio também — mas o `BaggagePropagator` já vem
no SDK OpenTelemetry, então o esquecimento exige esforço ativo (remover o
propagador) em vez de ser o comportamento padrão de quem não leu a convenção.

Há um terceiro fator que pesou: a chave de negócio precisa viajar por caminhos
onde **ninguém no meio sabe o que ela significa** — gateway, broker, adaptador.
Um header com nome próprio tende a ser riscado por qualquer política de
normalização de cabeçalho ("não conhecemos esse header"), enquanto `baggage` é um
cabeçalho padrão de transporte de contexto, conhecido por proxy, mesh e biblioteca
HTTP.

## Decisão

**Usar W3C Baggage como veículo e o header proprietário como interface de borda.**
Concretamente:

- `sre.correlation_id` é a **chave canônica** do ponto de vista de contrato. Ela
  vive no baggage e é promovida a atributo de span (`correlation.id`), a atributo
  de log (`correlation_id`) e a campo de consulta.
- `X-Correlation-Id` continua sendo aceito **na entrada** e devolvido **na
  resposta**, por três razões práticas: quem chama um serviço de pagamento
  (front-end, parceiro, canal) não tem SDK OTel; o time de suporte precisa
  colar essa string num ticket; e o corpo de resposta de API pública já costuma
  expor essa chave para o cliente.
- Ordem de resolução fixada em código e revisável num só lugar:
  **header de entrada → baggage → geração na borda**. Quem gera ID novo no meio
  da jornada é proibido (ver consequência 3).
- O `traceparent` continua sendo o contexto de trace dentro do mundo OTel. Ele
  **não** é a chave de correlação: com sampling abaixo de 100%, o trace pode nem
  existir, e um incidente nunca pode depender de uma amostra.

No repositório: `src/instrumentacao_fastapi.py` (funções `resolver_contexto`,
`montar_baggage`) e `src/fronteira_transacional.py` (função
`interpretar_baggage`, um parser de ~10 linhas — a fronteira não tem SDK, e
precisa entender o cabeçalho com o runtime que ela tem).

## Consequências

**Ganha-se:**

1. Propagação **gratuita** nos elos com SDK: `CompositePropagator` com
   `W3CBaggagePropagator` já carrega a chave em toda chamada saída HTTP/gRPC
   instrumentada. Nenhum time precisa lembrar de copiar um header.
2. A chave chega ao lado transacional **acompanhada de contexto de jornada**
   (`sre.jornada`, `sre.estagio`). Sem isso, do outro lado da fronteira você tem
   um ID órfão, sem saber a qual caso de negócio ele pertence — e é essa
   informação que faz a investigação andar.
3. A **origem** do ID passa a ser metadado (`origem_correlation_id = cliente |
   baggage | gerado-na-borda`). Isso transforma "quem perdeu a chave?" em série
   temporal, e não em conversa de corredor.
4. Padronização por padrão: quem instrumenta um serviço novo não precisa
   consultar wiki nenhuma — é o comportamento do SDK.

**Paga-se:**

1. **Baggage é texto plano e viaja em todo request.** Cabeçalho grande custa
   throughput e esbarra em limite de header de proxy (~8 KB; em CDN e gateway de
   API às vezes 4 KB). Mitigação aplicada: três pares curtos, valores
   controlados, proibição explícita de PII — `pan`, `cvv`, `cpf`, `cnpj`, nome de
   cliente, valor de transação **não** entram no baggage sob nenhuma hipótese
   (em meio de pagamento isso é requisito de conformidade, não preferência
   estética).
2. **Baggage é não-confiável por especificação.** Qualquer um pode forjar. Por
   isso ele nunca é usado para autorização, decisão de negócio ou idempotência de
   pagamento — só para correlação. Uma chave de correlação forjada é um problema
   de investigação; uma chave de autorização forjada é incidente de segurança.
3. **Não é criptografado nem assinado.** Se um dia a correlação precisar ser
   prova (auditoria regulatória de qual sistema processou qual transação),
   baggage não serve: aí é `traceparent` + registro assinado no serviço dono da
   transação, e esta decisão precisa ser revisitada.
4. O lado transacional precisa de um parser próprio (o `interpretar_baggage` de
   10 linhas), porque não existe runtime COBOL/legado com OTel. Isso é código que
   alguém vai ter que manter — e é exatamente o preço de não ter padrão do outro
   lado.

**Quando eu trocaria esta decisão:**

- se o estate ficar 100% com SDK OTel e o último elo sem tracing nativo for
  aposentado, o header de negócio continua valendo (é a chave que o cliente vê),
  mas o baggage pode virar atributo de recurso em vez de cabeçalho de transporte;
- se aparecer requisito de **não repúdio** da correlação, troca-se por token
  assinado (JWT de escopo interno, com expiração curta) — e paga-se o custo de
  tamanho de header e gestão de chave;
- se um gateway no caminho riscar `baggage` por política (acontece), a alternativa
  imediata é levar `sre.correlation_id` como atributo de contexto OpenTelemetry
  propagado por header próprio registrado no `CompositePropagator` — a chave de
  contrato não muda, só o veículo.

## Como reavaliar

Medir, e não opinar:

- `[medir: fração de spans com atributo correlation.id presente por camada, por
  semana]` — se cair em uma camada específica, o problema é aquele elo, não o
  padrão escolhido;
- `[medir: tamanho médio e p99 do cabeçalho baggage em bytes]` — a decisão só é
  barata enquanto couber no limite do gateway mais restritivo do caminho;
- `[medir: horas de investigação economizadas por incidente em que a chave foi
  necessária]` — que é o único número que justifica o trabalho de manter o parser
  do lado transacional.
