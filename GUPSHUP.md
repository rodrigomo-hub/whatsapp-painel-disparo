# Conexões Gupshup

Cadastro em `/gupshup`, protegido pela sessão administrativa. App ID e chave API
validam a lista completa de templates. As credenciais ficam somente em `gup_apps`
no banco persistente e não são devolvidas pela listagem nem copiadas para tokens Meta.

O envio exige nome exato do aplicativo e WhatsApp remetente, além do template e
limites. O contato comercial de `/business` **não** é usado como remetente.
Sem descoberta confiável, o operador completa esses dois campos. Novos cadastros
ficam fora do rodízio até configurar o número. Limites iniciais são zero.

Nesta versão, envio de templates TEXT com parâmetros posicionais; mídia e
parâmetros nomeados não são liberados. Template aprovado é obrigatório.
Qualidade do número desconhecida é permitida **somente na Gupshup**, conforme
decisão do operador; a interface orienta verificar a qualidade no provedor.
Números Meta e VendeAI mantêm suas regras anteriores.

A atualização de templates participa da rotina existente de 15 minutos. Erro
de consulta ou cache com mais de 20 minutos impede envio até atualizar. Status
pausado/rejeitado/desativado/excluído impede envio. Pausa manual não é desfeita.
Horários, rodízio de oficiais, limites e lista negra são compartilhados. A resposta
202 confirma aceite do provedor, não entrega ao WhatsApp. Timeout/5xx são incertos
e não disparam fallback SMS nem repetição automática neste caminho.

Em "Configurar webhook", copie a URL protegida com segredo individual e habilite
eventos de sistema de conta e templates na Gupshup. A integração não substitui
automaticamente callbacks existentes. Eventos de restrição/desativação pausam
o número; restabelecimento não o reativa sozinho. Eventos repetidos são deduplicados
e eventos antigos não desfazem estado mais recente. Não receber eventos não prova
saúde. Esta versão guarda outros eventos, mas não concilia entrega/leitura do
provedor com o histórico de disparos.

Disparos mantêm `tipo_envio=gupshup`, aparecem como oficiais nos totais e usam
somente a tarifa Meta configurada como referência de custo (sem taxas Gupshup).

Documentação usada:
- https://docs.gupshup.io/reference/sending-text-template
- https://docs.gupshup.io/reference/get-all-templates-for-an-app
- https://docs.gupshup.io/docs/system-events

Validação: testes em `tests/test_gupshup.py` usam respostas simuladas; o teste real
de envio deve ser realizado somente a um destinatário autorizado, após confirmar
remetente, nome do aplicativo e aprovação do template.
