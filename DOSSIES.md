# Dossiês de atendimento

Rota `/dossies`: administrador usa a sessão do painel; equipe tem sessão própria de oito horas, senha PBKDF2 e autorização explícita por conta/conversa. A equipe não acessa APIs do painel. Download verifica autorização e SHA-256 e gera auditoria.

Coleta manual por ID/link. Automação opcional para novos eventos `LIQUIDATED_TO_CUSTOMER`, independente do banco. Não consulta Facta nem faz backfill. Busca por CPF ainda não implementada.

Worker único separado dos disparos, paginação `before` até resposta vazia, JSON original preservado, PDFs de mensagens e registros internos separados, anexos originais e manifesto. Anexos indisponíveis geram pendência; erro de paginação impede conclusão. Até três tentativas para falhas de coleta. Limites: 100 MB de respostas, 50 MB/anexo, 250 MB de conteúdo por coleta. Versões anteriores não são sobrescritas. O fingerprint compara conteúdo e metadados informados pelo CRM; mudanças de URLs assinadas também podem produzir nova versão.

Arquivos em `DB_FILE.parent/dossies`, no volume persistente. O ZIP recebe SHA-256 e, após a coleta, o worker envia somente esse hash ao OpenTimestamps. O recibo `*.zip.ots` fica separado do ZIP, pois incluí-lo no pacote mudaria o hash carimbado. O worker tenta atualizá-lo a cada seis horas até o calendário devolver a âncora Bitcoin. Isto ainda NÃO é backup externo nem armazenamento imutável. PDF é derivado; mensagens e anexos originais ficam no ZIP. PDF não distingue IA de humano quando o CRM não informa. Hash do pacote fica no banco, fora do próprio ZIP.

`LocalStorage.put/path` define o armazenamento inicial. `dossies_copias` reserva o catálogo de cópias por provedor (local/B2/Drive), chave, hash e verificação. `dossies_carimbos` guarda o estado, hash e recibo do OpenTimestamps. Integrações B2/Drive, replicação, retenção e restauração de catálogo ainda precisam ser implementados. Não criar credenciais fictícias nem marcar proteção externa sem cópia verificada.

Mídia só pode vir dos hosts CRM conhecidos ou dos hosts explícitos em `DOSSIE_MEDIA_HOSTS` (separados por vírgula); redirects são revalidados e nunca recebem o token CRM. Configure hosts de storage somente após verificar a origem. Backups existentes do banco não incluem automaticamente a pasta de arquivos.

Coleta não envia mensagem, não resolve conversa e não altera contatos. Captura apenas o conteúdo ainda disponível no CRM; ausência/edição anterior à coleta não pode ser recuperada ou comprovada por este módulo.
