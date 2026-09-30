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
de cada número e o horário. Sem canal disponível ou fora do horário, novas entradas
são recusadas com HTTP 200, `status: "erro"`, `aceito: false` e `motivo_codigo`, sem
inserção na fila. O n8n deve verificar o corpo, não apenas o código HTTP.
A mesma checagem respeita os números selecionados no lote. O fallback SMS configurado
continua sendo uma alternativa para entradas individuais.
Se o canal ficar indisponível depois da entrada, os pendentes terminam em `erro`:
não aguardam o número voltar e não são reenviados automaticamente. A indisponibilidade
no instante do envio também encerra o item. O agrupamento de cadastro permanece em
5 minutos; bloqueios da VendeAI ainda respeitam Retry-After enquanto houver canal disponível.

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


## Importacao de planilha e lote exclusivo

O painel aceita CSV (UTF-8 ou Windows-1252; separador virgula, ponto e virgula ou tab)
e Excel XLSX (primeira aba). TXT e JSON continuam disponiveis. Limites: 10 MB,
20.000 linhas e 100 colunas no Excel. Formulas devem ser substituidas por valores.
O modelo CSV pode ser baixado em `/api/lote/modelo.csv` (sessao do painel).

Colunas: `telefone` (ou `whatsapp`), `cpf`, `nome`, `nascimento` (DD/MM/AAAA),
`produto`, `campanha`, `email` e `variavel_1` ate `variavel_10`.
Telefone e CPF devem preferencialmente ser texto no Excel. Datas Excel sao convertidas.
CPF numerico inteiro e completado para 11 digitos; CPF informado passa por checksum.
Campos opcionais seguem os padroes da integracao; email ausente e gerado no cadastro.
A importacao mostra linhas validas, duplicadas e erros por linha. Telefones repetidos
sao normalizados e mantem a primeira ocorrencia. Havendo erros, corrija e reimporte.
A previa nao cadastra leads nem envia mensagens.

`POST /api/lote/arquivo` recebe multipart com `arquivo` e retorna a previa e `leads`.
`POST /api/lote/capacidade` recebe `numeros_selecionados` e calcula por numero o menor
saldo entre limite diario e limite da ultima hora, descontando envios com sucesso.
Pausados e qualidade RED tem capacidade zero. A soma desses menores saldos e o teto
atual do lote, tambem respeitando horario. Exemplo: dois numeros com 500/dia nao
significam 1.000 disponiveis se ja houve envios ou o limite de hora for menor.

A capacidade e conferida novamente em `POST /api/disparo-lote`, inclusive para JSON
ou TXT e chamadas diretas da API. Excesso retorna HTTP 409, `aceito:false` e
`motivo_codigo:capacidade_insuficiente`, sem aceitar parte silenciosamente.
So inicia lote com fila vazia. Enquanto houver item ativo com origem lote,
`/disparar` e novos lotes retornam HTTP 409 com `fila_ocupada`; nada e guardado para
enviar depois. `/api/pode-disparar` tambem informa o bloqueio. As admissoes sao
serializadas entre processos por arquivo de lock no volume do banco; concorrencia
imediata retorna `entrada_em_andamento`. No modo imediato, o lock dura toda a chamada.
Finalizacao, erro ou cancelamento de todos os itens libera novas entradas.
A protecao contra falta de canal continua: se perder disponibilidade, encerra o item.
O n8n deve tratar recusas explicitamente, sem repeticao automatica indiscriminada.

## Templates com e sem variaveis

Na busca de templates da Meta, templates de texto estatico sao identificados como
"Sem variaveis". Ao selecionar e salvar, o numero guarda o modo e o idioma escolhidos.
Tambem e possivel marcar "Template de texto sem variaveis" na edicao de um numero
existente. Nesse modo, o envio omite `components` e ignora a lista de parametros
recebida do n8n ou planilha. Campos do cadastro VendeAI continuam sendo enviados.

Templates com variaveis continuam usando `variaveis_template` na ordem fornecida.
Se a lista estiver ausente ou vazia, o payload tambem omite `components`.
A migracao preserva o comportamento dos numeros existentes; para reconhecer um
template ja cadastrado, busque/selecione novamente ou marque explicitamente o modo.
A deteccao automatica nao classifica cabecalho de midia, botoes dinamicos ou templates
de autenticacao como texto sem variaveis. O modo nao adiciona suporte a midia nem a
parametros de cabecalho/botoes; esses formatos requerem implementacao especifica.

## Transbordo opcional entre paineis

Em Configuracoes > Transbordo, configure nome, URL base HTTPS e chave API do destino.
A chave nao e retornada no GET de configuracao; campo vazio preserva a chave salva.
A opcao nasce desligada. O teste de conexao consulta disponibilidade sem enviar leads.

Somente novas entradas individuais em `/disparar` podem transbordar, quando nao ha
canal local disponivel (WhatsApp e fallback SMS). Horario fechado, lote exclusivo,
falha de cadastro VendeAI, erros de dados e itens ja na fila nao acionam transbordo.
A prioridade continua local. `/api/pode-disparar` consulta o destino quando necessario.

O encaminhamento ocorre antes de cadastrar na VendeAI da origem. O destino precisa
ter VendeAI e fila habilitadas, credenciais proprias e disponibilidade. Ele usa seus
numeros e templates. Dados e variaveis recebidas seguem junto; campos nao informados
usam os padroes do destino. Templates devem ser compativeis com as variaveis ou
configurados como estaticos. Nao ha compartilhamento de banco nem tokens da Meta.

O recebedor `/api/transbordo/receber` exige X-API-Key e nunca encaminha novamente.
Uma recusa retorna `status:recusado`, `aceito:false`, sem fila local ou fallback.
Um aceite retorna `status:encaminhado`, `aceito:true`, `transbordo_id` e `consultar_em`.
Isso confirma recebimento no destino, nao envio de mensagem. Consulte
`GET /api/transbordo/{id}` com a chave da origem para atualizar o resultado remoto.
O painel mostra os encaminhamentos separadamente e oferece o botao Consultar.

Recomenda-se uma Idempotency-Key estavel por entrada do n8n. A mesma chave com dados
diferentes gera 409. Sem chave, entradas iguais sao deduplicadas por 24h; pendencias
e resultados incertos continuam protegidos. Um registro persistente e gravado antes
da chamada. Timeout, 5xx e resposta inesperada ficam sem confirmacao (`status:pendente`,
`aceito:null`); nunca geram novo POST ou envio local automatico. Consultar o recibo
pode recuperar um aceite cuja resposta se perdeu. Um 404 nao prova que o envio nao
ocorreu, portanto nao libera repeticao automatica. Troca de destino e bloqueada enquanto
houver encaminhamentos pendentes ou incertos. Redirects nao sao seguidos com dados/chave.

O JSON de entrada do n8n permanece igual, mas o fluxo deve tratar `encaminhado`,
`recusado` e `pendente` explicitamente. Nao repetir com nova chave apenas por timeout.
Esta implementacao protege a transferencia entre paineis; nao promete exatamente uma
entrega final do WhatsApp em falhas externas. A politica `incerto` da fila continua.

O painel de destino mostra as ultimas 50 tentativas autenticadas em Transbordos recebidos, inclusive recusas por capacidade, configuracao ou lote em andamento. Recusas nao entram na fila. Aceites acompanham o estado atual da fila. O historico comeca apos esta atualizacao; consultas de disponibilidade nao sao entradas de leads.

## Checagem antes da consulta paga (n8n)

GET /api/disponibilidade-consulta, autenticado com X-API-Key do painel de origem.
Nao recebe CPF nem telefone e nao chama Nova Vida, Meta ou cadastro de leads.
Prioriza a capacidade local (incluindo SMS se configurado); se esgotada, consulta
Simplifica pelo destino configurado e habilitado. Falha de comunicacao retorna
consultar_nova_vida=false. Respeita horario e exclusividade de lote da origem.
No n8n: HTTP Request -> IF consultar_nova_vida is true -> Nova Vida -> /disparar.
No ramo false, encerre o processamento antes da consulta paga.
A resposta informa destino_provavel, nome_destino, motivo_codigo e motivo_destino.
Nao reserva capacidade nem garante entrega: a entrada confere novamente.

## Templates com imagem

Ao buscar templates, cabecalhos IMAGE exibem o anexo obrigatorio JPEG/PNG (ate 5 MB).
O servidor confirma o template/idioma na Meta ao salvar; sem imagem a configuracao
nao e aceita. A imagem fica privada no banco persistente, com preview autenticado.
Uploads sao feitos ao endpoint /PHONE_ID/media; mensagens usam header image.id.
IDs de midia sao renovados apos 20 dias com a copia salva. Falha no upload impede
o envio da mensagem. Nao ha envio de teste durante a configuracao.
Trocar template ou idioma exige anexar novamente; editar o mesmo preserva a imagem.
Numeros com cabecalho IMAGE conhecido e sem imagem nao contam na capacidade local,
na planilha, na preconsulta paga ou na disponibilidade para receber encaminhamentos.
Cada painel usa sua propria imagem; o n8n nao precisa enviar imagem com cada lead.
Para numeros antigos, Atualizar todos identifica o cabecalho; depois use Editar,
Buscar templates, selecione o template e anexe a imagem.

## Reutilizar WABA no cadastro de numeros

O formulario continua adicionando um numero por vez. O campo WABA sugere contas
salvas; token vazio reutiliza a credencial no servidor. A lista de WABAs nao expoe
segredos. As WABAs dos numeros existentes sao importadas automaticamente.
Apos cadastrar, WABA, template, imagem e limites permanecem para o proximo numero.
Numeros ja cadastrados ficam desabilitados na busca e a API recusa duplicacao.
Um token preenchido substitui a credencial salva da WABA apos cadastro bem sucedido,
sem alterar tokens dos numeros existentes. Cada painel mantem suas proprias WABAs.

## Conexões normais da VendeAI

A aba `/conexoes` separa a descoberta de números da autorização para disparar. A descoberta cruza os inboxes do CRM com `accepts_free_text=true` no mailing; não cadastra números no rodízio. Para adicionar, o operador escolhe um modelo ativo e os limites diário e horário. As credenciais vêm da configuração VendeAI existente (override do painel antes do ambiente) e nunca são copiadas para a tabela de números.

Modelos locais aceitam `{primeiro_nome}`. São exclusivos das conexões normais; os templates da Meta continuam independentes. Desativar um modelo retira seus números da elegibilidade. Nome ausente bloqueia modelos que exigem a variável.

Os números importados usam o mesmo rodízio, limites, horários, mailing e lista negra. O status é consultado a cada 15 minutos, ao atualizar manualmente e imediatamente antes do envio. Conexão desconhecida, desconectada, exigindo reautorização ou com consulta mais antiga que 30 minutos não entra na capacidade. Revalidar a conexão não altera a pausa manual. Antes do POST, o sistema relê a pausa, o modelo e a supressão.

Envio: `/api/message-handler/mailing/send/`, com `payload.type=whatsapp_nao_oficial`, mensagem de texto e inbox escolhido. Timeout, resposta ambígua ou falha de servidor são registrados como `incerto`, sem repetir por outro número/SMS. O limite mantém essa tentativa reservada para evitar exceder a capacidade. A fila trata esse estado como terminal; confira o envio na VendeAI antes de qualquer reenvio manual.

Migração aditiva: campos de tipo/inbox/modelo/status em `numeros`, tabelas `conexoes_vendeai` e `modelos_normais`. Números existentes permanecem do tipo `meta`. A atualização do código não importa nem ativa números automaticamente.

Todo envio confirmado por conexão normal agenda a etiqueta `disparo` na conversa do CRM. A tarefa persistida em `etiquetas_disparo` cruza telefone, inbox e ID da mensagem (não escolhe apenas a última conversa), lê as etiquetas atuais e acrescenta `disparo`. A marcação roda em segundo plano, com três tentativas e backoff; falhas ficam no banco e em `eventos`, sem reenviar a mensagem. A etiqueta não é parte do texto e não é enviada no payload de mailing, que não documenta esse campo.

## Oficiais vinculados à VendeAI

A área dos números mantém os oficiais e oferece `/oficiais-vendeai` para descoberta/importação. `/conexoes` passa a se chamar **Conexões normais**, mantendo seus modelos locais separados. A descoberta consulta a VendeAI, associa automaticamente os oficiais existentes por identidade Meta, sem criar ou ativar números, e guarda somente metadados; nenhuma credencial é devolvida pela lista de importação.

A conciliação usa `phone_number_id` e WABA. IDs duplicados, WABA divergente ou inbox já associada a outra identidade impedem o vínculo. A associação automática de um existente preserva token, template, limites, histórico, caminho e pausa; novos oficiais recebem a credencial da conexão no servidor e são adicionados pausados para revisão. Não há ativação nem troca de caminho automática.

O campo **Enviar por** no editor oferece Meta ou VendeAI. A opção VendeAI exige vínculo e correspondência do template por nome, idioma, categoria, corpo, variáveis e cabeçalho. Qualidade, status do número, saúde da WABA e aprovação do template são consultados diretamente na Meta antes de cada envio por esse caminho. Divergência, reautorização, qualidade desconhecida ou bloqueio impedem o envio. Variáveis posicionais do n8n viram `params`; templates estáticos usam `{}`. Cabeçalho com variável, vídeo/documento, variáveis nomeadas e botões que exigem parâmetros não são aceitos neste adaptador e geram erro explícito.

Imagens nesse caminho ficam no banco e são expostas somente por URL com token aleatório de 256 bits, válida por uma hora, gerada no envio. O painel precisa estar em HTTPS. Não é necessário subir mídia à Meta ao salvar uma imagem para VendeAI; se o operador voltar ao envio direto, o upload à Meta é feito no primeiro uso. A rota de mídia não expõe IDs de banco nem tokens das integrações.

O envio usa o token CRM e `whatsapp_oficial`, agenda a etiqueta `disparo` e registra o ID retornado pela VendeAI. Falhas incertas mantêm o limite reservado e nunca repetem a mensagem pela Meta ou SMS. Os testes reais de implantação são consultas/leitura: nenhum número é migrado automaticamente e nenhum disparo real é feito sem destino autorizado para o teste.

Na tela de oficiais, números reconhecidos aparecem como “Já está no painel”, com “Editar” opcional. Somente os novos mostram “Adicionar ao painel”. A associação automática roda na busca manual e na rotina de atualização de 15 minutos; conexões duplicadas para a mesma identidade são sinalizadas, sem escolher uma arbitrariamente.
