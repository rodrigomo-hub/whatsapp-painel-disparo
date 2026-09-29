# Eventos da VendeAI e New

Publicação de 29/09/2026 aplicada **somente à Simplifica**. O painel de origem não recebeu esta publicação.

- `/eventos-vendeai`: consulta autenticada do JSON original dos últimos 100 recebimentos, preservando todo o histórico no banco.
- A URL secreta de recebimento é copiada na página. Substituir a URL antiga do n8n na VendeAI para migrar o fluxo; não manter os dois destinos encaminhando a mesma proposta.
- New: encaminha apenas `proposal_created`, produto `clt`, banco `facta`. O modelo e as credenciais ficam no banco, nunca neste repositório. O telefone vem do contato no webhook.
- Repetições ficam registradas, mas a chave conta/proposta evita novo encaminhamento. Dados incompletos podem ser corrigidos por novo evento. Timeout ou reinício durante envio exige conferência no New, sem reenvio automático.
- `ENVIADO_HTTP` indica resposta HTTP bem-sucedida, não confirmação comercial de cadastro. A resposta fica disponível para inspeção.
- A captura exige a chave da URL e a conta configurada. Páginas e APIs de consulta exigem sessão administrativa.
- Cada número exibe `meta_consultado_em`, horário de Brasília da consulta de qualidade/status, inclusive tentativas sem resposta confirmada. Consultas antigas não são retroativamente datadas; a coluna começa vazia. Recarregar a página não altera a data. A rotina existente continua a cada 15 minutos e também consulta após envios/atualizações manuais.

Validação: 157 testes existentes e 8 testes adicionais da integração; JavaScript validado com `node --check`. Nenhuma proposta real enviada durante os testes. A substituição do webhook na VendeAI ainda é necessária para iniciar o recebimento.
