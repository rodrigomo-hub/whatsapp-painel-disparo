# Lembretes de assinatura

Página `/lembretes-sms`, API autenticada GET/PUT `/api/lembretes-sms`.
Instalação inicia desativada; a ativação é por painel e usa sua credencial SMS.
Dados vêm exclusivamente dos webhooks confirmados da conta atual. Não há consulta
de status em tempo real: atraso no webhook pode permitir um lembrete após a assinatura.

Padrão: 30 minutos desde a criação, 4 horas desde o primeiro SMS, 24 horas desde
o segundo. Configuráveis até 3 etapas e idade máxima de 7 dias (1–30).
Propostas antigas começam na primeira etapa, nunca recebem todas de uma vez.
Somente Facta, status FORMALIZATION_REQUESTED, celular brasileiro e URL HTTPS
facta.ly. Links recebidos sem protocolo são normalizados. Mensagens nunca são truncadas.
Respeita horário de funcionamento e optout; no máximo 3 SMS por telefone/24h,
com intervalo mínimo de 4h por telefone, inclusive entre propostas diferentes.

Histórico é projetado antes de qualquer decisão. Reconfere eventos, configuração,
status, link, telefone e optout antes do POST. Eventos terminais são persistentes
para impedir reabertura por webhook atrasado. Reserva atômica por conta/proposta/etapa;
falha/incerto interrompe a sequência e não repete automaticamente. Reinício marca
envios em andamento como incertos. O limite por telefone é específico dos lembretes,
não inclui campanhas SMS independentes.

Relatório: período seleciona primeiro SMS aceito por proposta; formalização e pagamento
são contados por primeiro aviso recebido depois do SMS, até 7 dias, uma vez por proposta.
Não infere assinatura a partir do pagamento. Sem confirmação de entrega de SMS,
sem atribuição causal. Os SMS também ficam em disparos, canal sms,
tipo_envio lembrete_assinatura. A estimativa de custo Meta não se aplica.
