# Painel de disparos — cadastro de campanha VendeAI

Antes de enviar uma mensagem, o painel pode cadastrar os dados conhecidos do lead
em `POST https://ia.vendeaitecnologia.com.br/api/message-handler/mailing/leads/`.
O telefone vincula o cadastro ao futuro primeiro contato com a IA.
Esta integração substitui a antiga sincronização automática de contatos Chatwoot.

## Configuração no painel

Na aba **Configurações → Base de campanha**, configure:

- Habilitar/desabilitar o cadastro antes dos disparos.
- URL base: `https://ia.vendeaitecnologia.com.br` (sem caminho).
- Account ID da conta e seu **token de API do CRM**.
- Produto padrão (`clt` inicialmente; use um produto válido na sua conta).
- Campanha padrão opcional.
- Fila automática para novas entradas e intervalo entre rodadas (padrão: **300 segundos / 5 minutos**).
- Política de falha **somente para o modo sem fila**: não disparar ou continuar sem cadastro confirmado.
  Na fila, a confirmação de cadastro é sempre obrigatória, inclusive para fallback SMS.

O token digitado no painel é salvo no banco persistente, não é devolvido pela API
de configuração e não aparece nos eventos. Campo vazio mantém o valor salvo.
A opção de remover o token do painel volta a usar `VENDEAI_API_TOKEN`, se definido
no ambiente do servidor. Essa variável continua sendo uma alternativa ao painel.
Proteja o banco e seus backups como credenciais; o valor armazenado não é criptografado.

Na atualização, as configurações existentes são preservadas e as novas colunas são
criadas automaticamente. A URL de campanha é independente da antiga `vendeai_url`
do CRM. Revise o produto padrão e a política de falha antes de retomar uma integração
que já estava habilitada. `vendeai_inbox_id` não é necessário para cadastro de campanha.
A rota legada de atributos de conversa permanece disponível, mas não é usada no disparo.

## Fila automática (produção: habilitada, 5 minutos)

O corpo de `POST /disparar` e a autenticação do n8n continuam iguais. Com VendeAI e
fila habilitadas, a entrada é salva no banco e responde **HTTP 202**, sem enviar mensagem:

```json
{
  "status": "enfileirado",
  "fila_id": "id-do-item",
  "estado_fila": "aguardando_cadastro",
  "duplicado": false,
  "consultar_em": "/api/fila/id-do-item",
  "vendeai": {"status": "aguardando"}
}
```

O primeiro lead inicia uma janela de 5 minutos. Cada rodada cadastra até 20.000
telefones em uma única chamada, incluindo entradas individuais e lotes. Rodadas vazias
não chamam a VendeAI. Depois do cadastro, o envio segue o rodízio, a qualidade, os limites
de cada número e o horário. A espera pode ultrapassar 5 minutos se não houver número
livre, se estiver fora do horário, se houver mais de 20.000 leads ou bloqueio da API.

No n8n, trate `enfileirado` como **entrada recebida**, não como mensagem enviada. Para
acompanhar, use `GET /api/fila/{fila_id}` com o mesmo header `X-API-Key`. O endpoint
retorna a etapa em `status` e a resposta final do envio em `resultado`. Não é necessário
reenviar os itens que estão aguardando. O painel mostra a fila, erros e próxima rodada.

Etapas: `aguardando_cadastro`, `cadastrando`, `pronto` (aguardando envio), `enviando`,
`sucesso`, `erro`, `incerto` e `cancelado`. Cada rejeição da VendeAI é associada ao lead;
os demais cadastros aceitos seguem. Erros definitivos de dados exigem corrigir a entrada.

Em HTTP 429, a fila aguarda pelo menos o `Retry-After` informado pela VendeAI antes de
uma nova chamada, e continua recebendo leads. Falhas de conexão/5xx e de credencial
ficam guardadas para nova rodada; corrija a credencial no painel se necessário. A fila
não cria uma cota local. Agrupar a cada 5 minutos usa até 12 chamadas/hora, mas outras
aplicações na mesma conta também consomem a cota externa.

A fila persiste no `DB_PATH`. O trabalhador usa um arquivo `.fila.lock` no mesmo volume,
para impedir dois processos de enviar simultaneamente durante reinícios/deploys. Um
cadastro interrompido pode ser repetido por upsert. Um envio interrompido fica `incerto`
e não é repetido automaticamente, pois a Meta pode já ter recebido a mensagem.
Confira o histórico antes de decidir por um novo envio. Não há garantia distribuída de
"exatamente uma vez" para efeitos externos.

Retries idênticos da entrada dentro de 24 horas retornam o mesmo ID. Itens ainda ativos
ou incertos continuam deduplicados após esse período. O e-mail aleatório é mantido no
item salvo e não participa da deduplicação quando foi gerado. Para uma identidade
explícita, envie o header `Idempotency-Key` (individual) ou o campo `idempotency_key`
(individual/por lead no lote), de 1 a 200 caracteres. Uma chave já usada com outro
conteúdo retorna 409. Uma chave explícita permanece associada ao item salvo. Para
um novo envio intencional do mesmo conteúdo, use uma chave nova.

Fila e leads legados são separados: nenhum disparo antigo é colocado na fila ou
reenviado. O painel permite cancelar apenas itens aguardando cadastro ou envio.
Desativar a opção de fila muda **novas entradas** para o modo imediato; os itens já
recebidos continuam. Desabilitar a integração VendeAI pausa os itens existentes.
Trocar conta/URL exige concluir ou cancelar os pendentes para não enviar dados à conta
errada. A migração preserva a configuração antiga e cria `fila_habilitada=0`; nesta
implantação, a fila é explicitamente ativada com intervalo de 300 segundos.

## Disparo individual

`POST /disparar` — autenticação existente: header `X-API-Key` com a `API_KEY` do painel,
ou sessão de administrador. **Não envie o token da VendeAI no corpo de cada disparo.**

Exemplo fictício de entrada:

```json
{
  "whatsapp": "11987654321",
  "cpf": "11144477735",
  "nome": "Lead de exemplo",
  "nascimento": "1992-11-23",
  "email": "lead@example.com",
  "produto": "clt",
  "campanha": "campanha_setembro",
  "variaveis_template": ["Lead"]
}
```

| Campo | Aceito também | Regra |
| --- | --- | --- |
| `whatsapp` | `telefone`, `phone` | Obrigatório; texto com celular brasileiro, com ou sem `55` |
| `cpf` | — | Opcional; texto, com ou sem pontuação; a VendeAI valida checksum |
| `nome` | `name` | Opcional; nome completo |
| `nascimento` | `birth_date` | Opcional; `AAAA-MM-DD` ou `DD/MM/AAAA`; enviado como `DD/MM/AAAA` |
| `email` | — | Opcional; quando ausente, gera endereço aleatório `<primeiro nome><5 dígitos aleatórios>@gmail.com` |
| `produto` | `product` | Substitui o produto padrão; obrigatório na VendeAI |
| `campanha` | `campaign` | Substitui a campanha padrão |
| `variaveis_template` | — | Lista de textos para o template WhatsApp; continua independente do cadastro |

`info_extra.nascimento`, `info_extra.birth_date` e `info_extra.email` também são
aceitos quando não informados na raiz. E-mail informado é preservado; se ausente,
nulo ou vazio, gera um endereço com o primeiro nome em minúsculas, sem acentos ou sinais, e cinco
dígitos aleatórios (ex.: `jose48217@gmail.com`), sem usar CPF ou telefone. Sem nome
utilizável, usa `lead` no lugar do primeiro nome. Isso
apenas preenche o cadastro, não cria uma conta Gmail nem verifica uma caixa postal.
Cada nova entrada sem e-mail gera outro endereço, inclusive em reenvios.
Os demais campos opcionais vazios são omitidos.
`nomeMae` e outros atributos livres não são enviados: o endpoint documentado não os aceita.
Entradas antigas com apenas telefone, nome e CPF continuam funcionando usando os padrões.
No modo imediato (fila desligada), uma lista com um único lead é enviada à VendeAI.
Na fila, entradas individuais são agrupadas conforme descrito acima.

No modo imediato, a resposta mantém os campos do disparo e acrescenta:

```json
{"vendeai": {"status": "cadastrado", "validade_dias": 7}}
```

Se a integração estiver desligada, `vendeai.status` será `desabilitado`.
Sem fila, se ocorrer falha com a política bloquear, nenhuma mensagem WhatsApp/SMS será enviada:

```json
{
  "status": "pendente",
  "motivo_codigo": "vendeai_cadastro_falhou",
  "motivo": "Disparo nao realizado: cadastro na VendeAI nao confirmado",
  "vendeai": {
    "status": "erro",
    "motivo": "HTTP_429",
    "http_status": 429,
    "tentar_apos_segundos": 42
  }
}
```

No modo imediato, o chamador deve verificar `status` e `vendeai`, não apenas HTTP 200. `pendente` não
é uma fila: o chamador precisa corrigir os dados ou reenviar após a espera informada.
Não repita envios já marcados como `sucesso`. No modo `continuar`, o cadastro pode
falhar e a mensagem ser enviada; o erro continua visível em `vendeai`.

## Disparo em lote

`POST /api/disparo-lote` aceita a entrada antiga `numeros_texto` ou o novo array
`leads` (não ambos). Cada lead aceita os mesmos campos do individual.

```json
{
  "leads": [
    {"telefone": "11987654321", "nome": "Lead A", "produto": "clt", "nascimento": "23/11/1992"},
    {"telefone": "11987654322", "nome": "Lead B", "produto": "fgts"}
  ],
  "numeros_selecionados": []
}
```

O painel também aceita essa lista JSON na área de lote ou em arquivo `.json`.
Até 20.000 itens por requisição. Telefones repetidos são normalizados e deduplicados;
o primeiro item prevalece. Com fila, retorna HTTP 202 com IDs por item; o trabalhador
agrupa os elegíveis na próxima rodada. Sem fila, o lote elegível usa uma chamada à VendeAI. Uma rejeição por CPF, nascimento, telefone, e-mail ou produto não impede
o disparo dos outros leads aceitos. O resultado de cada telefone inclui `vendeai`.
Dados de entrada com tipos incorretos retornam HTTP 400 antes de qualquer envio.
O lote mantém o comportamento anterior de envio WhatsApp pelo rodízio selecionado;
o fallback SMS continua pertencendo ao endpoint individual.

## Limites e validade

- A VendeAI documenta 10 chamadas/minuto e 1.440 chamadas/dia por conta. O painel
  não aplica cota local: tenta o cadastro na API, mesmo quando a contagem histórica
  local excede esses valores. Somente a VendeAI determina se sua cota foi atingida.
- Outras aplicações na mesma conta também consomem o limite da VendeAI. HTTP 429
  retorna uma indicação de espera. A fila aguarda esse prazo e repete apenas o cadastro;
  o modo imediato não faz retry automático.
- Timeout, HTTP 403/429/5xx ou resposta inconsistente nunca são tratados como cadastro confirmado.
- Leads expiram após **7 dias**. Reenviar atualiza os dados e renova a validade.
  Itens já cadastrados mas ainda não enviados pela fila são recadastrados se a validade
  expirar antes do envio. Leads já enviados não são renovados automaticamente. Não use
  `/disparar` apenas como renovação, pois ele solicita um envio.
- O cadastro se destina ao primeiro contato futuro com a IA. Não valida preenchimento
  retroativo de conversas existentes nem garante que a IA simule todo produto automaticamente.

## Desenvolvimento e atualização

Python 3.12. Para testes locais:

```sh
python -m venv venv
# Ative o ambiente virtual conforme seu sistema operacional.
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Os testes usam banco temporário e simulam VendeAI/Meta: não enviam mensagens reais.
Em Docker, os módulos `vendeai_mailing.py` e `fila_disparos.py` são incluídos na imagem. Mantenha `DB_PATH`
em volume persistente. Após publicar o código e atualizar o serviço, confira o cartão
de configuração e teste com um próximo lead autorizado. Nenhuma credencial real deve
ser incluída em commits, exemplos ou arquivos de teste.
