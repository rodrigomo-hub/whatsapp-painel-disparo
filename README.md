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
- Política de falha: **não disparar** (padrão) ou **continuar sem cadastro confirmado**.

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
| `email` | — | Opcional |
| `produto` | `product` | Substitui o produto padrão; obrigatório na VendeAI |
| `campanha` | `campaign` | Substitui a campanha padrão |
| `variaveis_template` | — | Lista de textos para o template WhatsApp; continua independente do cadastro |

`info_extra.nascimento`, `info_extra.birth_date` e `info_extra.email` também são
aceitos quando não informados na raiz. Campos opcionais vazios são omitidos.
`nomeMae` e outros atributos livres não são enviados: o endpoint documentado não os aceita.
Entradas antigas com apenas telefone, nome e CPF continuam funcionando usando os padrões.
Uma lista com um único lead é enviada à VendeAI no disparo individual.

A resposta mantém os campos do disparo e acrescenta:

```json
{"vendeai": {"status": "cadastrado", "validade_dias": 7}}
```

Se a integração estiver desligada, `vendeai.status` será `desabilitado`.
Se ocorrer falha no modo padrão, nenhuma mensagem WhatsApp/SMS será enviada:

```json
{
  "status": "pendente",
  "motivo_codigo": "vendeai_cadastro_falhou",
  "motivo": "Disparo nao realizado: cadastro na VendeAI nao confirmado",
  "vendeai": {
    "status": "erro",
    "motivo": "LIMITE_LOCAL",
    "tentar_apos_segundos": 42
  }
}
```

O chamador deve verificar `status` e `vendeai`, não apenas HTTP 200. `pendente` não
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
o primeiro item prevalece. O lote inteiro de leads elegíveis usa uma única chamada
à VendeAI. Uma rejeição por CPF, nascimento, telefone, e-mail ou produto não impede
o disparo dos outros leads aceitos. O resultado de cada telefone inclui `vendeai`.
Dados de entrada com tipos incorretos retornam HTTP 400 antes de qualquer envio.
O lote mantém o comportamento anterior de envio WhatsApp pelo rodízio selecionado;
o fallback SMS continua pertencendo ao endpoint individual.

## Limites e validade

- 10 chamadas/minuto e 1.440 chamadas/dia por conta. O controle local usa SQLite,
  reserva atômica e janelas móveis de 60 segundos/24 horas; persiste entre reinícios.
- Outras aplicações na mesma conta também consomem o limite da VendeAI. HTTP 429
  retorna uma indicação de espera, sem retry automático dentro do disparo.
- Timeout, HTTP 403/429/5xx ou resposta inconsistente nunca são tratados como cadastro confirmado.
- Leads expiram após **7 dias**. Reenviar atualiza os dados e renova a validade.
  Não há renovação automática nem agendamento nesta implementação. Reenviar para
  `/disparar` também tenta enviar outra mensagem; não o use apenas como renovação.
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
Em Docker, o novo módulo `vendeai_mailing.py` é incluído na imagem. Mantenha `DB_PATH`
em volume persistente. Após publicar o código e atualizar o serviço, confira o cartão
de configuração e teste com um próximo lead autorizado. Nenhuma credencial real deve
ser incluída em commits, exemplos ou arquivos de teste.
