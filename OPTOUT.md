# Opt-out — painel de origem

Ativado somente no painel de origem (conta 33393). Simplifica não recebe esta publicação.

- `/optout`: lista negra, textos configuráveis e histórico operacional. Sessão administrativa obrigatória.
- Gatilho atual: `conversation_created.first_message`, comparação exata após trim, minúsculas e remoção de acentos. `message_created` não foi observado nos eventos recebidos; nenhum campo de mensagem é presumido.
- O webhook autenticado e da conta configurada grava evento, supressão e tarefa na mesma transação antes de responder 200. Não faz chamadas ao CRM na resposta.
- Um worker é reservado para resolver conversas; outros três ajudam na resolução e executam os demais passos. Consultas periódicas à Meta saem do event loop para não atrasar a recepção.
- Ordem: resolver explicitamente, obter contact_id, desatribuir, ler/preservar labels e acrescentar `nao_quer_mensagens`, excluir contato, verificar 404. Uma resolução não confirmada impede exclusão. Um contact_id desconhecido nunca é adivinhado.
- Opt-out bloqueia API, planilha, WhatsApp, SMS, mailing e transbordo. Fila pendente é cancelada e há barreira final no envio. Mensagem já entregue ao provedor não pode ser desfeita.
- Credencial em `VENDEAI_API_TOKEN`, base HTTPS fixa do CRM. Nenhum token nos registros de execução.
- Três tentativas por etapa em 429/5xx/falha de comunicação, com backoff e Retry-After; falhas permanentes ficam para revisão. Retomada após reinício usa as operações idempotentes e estado persistido. Erros posteriores não removem supressão.
- Logs em `optout_passos`: conversa, operação, HTTP, duração da chamada e tempo desde recebimento. Sem copiar resposta contendo dados pessoais ou credenciais.
- A latência externa e o início da IA na VendeAI não são controlados pelo painel; encerramento abaixo de 2s é alvo, não garantia.

Validação: 177 testes na suíte; testes específicos de opt-out e planilhas repetidos após ajustes. CRM consultado somente por GET para verificar credencial. O fluxo destrutivo é coberto por mocks; não foi executado em conversa de cliente como teste. A validação real completa depende de conversa de teste identificada pelo usuário.

## Teste de exclusão direta

`POST /api/optout/testar-exclusao` arma uma tentativa única para o próximo novo opt-out elegível. Duplicatas não consomem o teste. A supressão continua sendo persistida antes de qualquer chamada externa. Nesse modo, a primeira operação é `DELETE /conversations/{chat_id}`, seguida de GET para confirmar 404. Não há consulta prévia nem exclusão do contato nesse teste. Se o DELETE for recusado após as tentativas previstas, retoma-se o fluxo padrão. Um HTTP 200 no DELETE não é registrado como exclusão confirmada sem o GET 404. A eficácia para impedir a resposta da IA depende do teste real.
