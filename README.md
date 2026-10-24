# Bot de moderação para Discord

Bot de moderação em Python com `discord.py 2.x`. Os comandos estão disponíveis
como comandos slash (por exemplo, `/kick`) e também com o prefixo `e!`
(por exemplo, `e!kick`).

## Requisitos

- Python 3.10 ou mais recente
- Uma aplicação e um bot criados no [Discord Developer Portal](https://discord.com/developers/applications)

## Configuração segura do token

1. No Developer Portal, crie uma aplicação, adicione um bot e copie o token na
   página **Bot**.
2. Na página **Installation** ou **OAuth2 > URL Generator**, gere um convite
   com os escopos `bot` e `applications.commands`. Conceda ao bot apenas as
   permissões necessárias para os recursos que usará: expulsar, banir, moderar membros,
   gerenciar mensagens, ver histórico, gerenciar canais, gerenciar permissões
   de canais e enviar mensagens/embeds no canal de logs. **Ver registro de
   auditoria** permite identificar moderadores em ações feitas fora do bot.
3. Copie `.env.example` para `.env` e coloque o token somente no arquivo `.env`:

   ```dotenv
   DISCORD_TOKEN=seu_token_real_aqui
   ```

   No PowerShell:

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

4. O `.gitignore` já exclui `.env`, o banco de dados e caches do Python. Nunca
   remova a regra `.env` nem publique o token. Se o token for exposto, redefina-o
   imediatamente no Developer Portal.

O arquivo `.env.example` contém apenas um valor de exemplo e pode ser versionado;
ele não deve conter o token real.

## Notificações de lives da Twitch

Para consultar lives automaticamente, crie uma aplicação no
[Twitch Developer Console](https://dev.twitch.tv/console/apps) e configure o
ID e o segredo do cliente no `.env` do bot:

```dotenv
TWITCH_CLIENT_ID=seu_client_id
TWITCH_CLIENT_SECRET=seu_client_secret
```

Essas credenciais são privadas: não as envie no Discord nem as publique no Git.
O bot usa o fluxo OAuth de aplicação da Twitch e consulta os streamers
cadastrados a cada 60 segundos. O cadastro e o estado da última live anunciada
ficam no SQLite, evitando anúncios repetidos durante a mesma transmissão.

Use os comandos slash, disponíveis para quem tem **Gerenciar servidor**:

- `/stream-add streamer canal` cadastra ou atualiza o canal de anúncio.
- `/stream-remove streamer` remove o streamer monitorado.
- `/stream-list` lista os cadastros do servidor.
- `/stream-test streamer` publica um anúncio de teste no canal cadastrado.

Informe o nome de usuário da Twitch sem URL, por exemplo
`/stream-add rheccha #anuncios`. O bot precisa poder ver o canal, enviar
mensagens e incorporar links. O primeiro monitoramento pode anunciar uma live
que já esteja no ar; novas sessões são anunciadas uma vez cada.

## Instalar e executar

Antes de iniciar o bot, instale o FFmpeg no sistema operacional e garanta que o
executável `ffmpeg` esteja disponível no PATH:

- Windows: baixe a versão estável do FFmpeg e adicione a pasta `bin` ao PATH.
- Linux/macOS: use o gerenciador do sistema (`sudo apt install ffmpeg` ou equivalente).

No PowerShell, a partir da pasta do projeto:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Se a política do PowerShell bloquear a ativação, use diretamente
`.\.venv\Scripts\python.exe -m pip install -r requirements.txt` e
`.\.venv\Scripts\python.exe run.py`.

Para reproduzir áudio, o bot depende de `yt-dlp` e do FFmpeg, além das
permissões para entrar e falar em um canal de voz. Use `/play <busca>` ou
`e!play <busca>` para buscar uma música pelo nome ou informar um link.
Use `/twitch` ou `e!twitch` para reproduzir uma live pública.

O YouTube pode rejeitar servidores por verificações anti-bot relacionadas ao IP
ou à reputação do host. Se isso ocorrer e você tiver autorização para acessar
o conteúdo, configure um arquivo de cookies válido com `YTDLP_COOKIES_FILE` no
`.env` do servidor. Cookies são credenciais privadas: não os envie no Discord
nem os adicione ao Git. A configuração de cookies não é necessária para usar
o comando de live da Twitch.

Use `/twitch <canal>` ou `e!twitch <canal>` com o nome do canal ou o link de
uma live pública da Twitch, por exemplo `e!twitch rheccha` ou
`e!twitch https://www.twitch.tv/rheccha`. O bot reproduz somente o áudio pelo player de voz;
use `/stop` para encerrar a reprodução ou `/leave` para encerrar e desconectar.
A desconexão automática por inatividade fica suspensa durante a live. A
reprodução termina se a transmissão acabar ou ficar indisponível.

O player desconecta automaticamente após cinco minutos sem atividade de quem
adicionou a última faixa. O temporizador é reiniciado quando essa pessoa usa
um comando ou botão de música. `/leave` encerra a reprodução e desconecta
imediatamente.

Ao iniciar, o bot remove registros locais antigos para evitar comandos
duplicados e sincroniza a lista atual no escopo global. O Discord pode levar
até uma hora para propagar comandos globais novos. Reinicie o processo do bot
depois de atualizar os arquivos. Servidores aos quais o bot for adicionado
depois também têm registros locais antigos removidos. O banco SQLite é criado
em `data/moderation.db`.

## Comandos

| Comando | Descrição |
| --- | --- |
| `/kick membro motivo` | Expulsa um membro |
| `/ban membro motivo` | Bane um membro |
| `/unban user_id motivo` | Remove o banimento pelo ID numérico do usuário |
| `/timeout membro duracao motivo` | Aplica timeout; duração como `30m`, `2h` ou `3d`, até 28 dias |
| `/untimeout membro motivo` | Remove o timeout |
| `/warn membro motivo` | Registra um aviso persistente |
| `/avisos membro` e `/warnings membro` | Consulta os 25 avisos mais recentes |
| `/clearwarns membro` | Remove avisos ativos sem apagar os casos de auditoria |
| `/case caso` | Consulta um caso de moderação pelo número |
| `/clear quantidade` | Apaga de 1 a 100 mensagens recentes do canal |
| `e!clearglobal message_id` | Apaga a mensagem indicada e as cópias de texto exato, do mesmo autor, em todos os canais de texto e voz |
| `/ajuda` | Mostra somente os comandos disponíveis para quem executou |
| `/userinfo [membro]` | Mostra informações públicas do membro; sem argumento, mostra você |
| `/ping` | Mostra a latência atual do bot |
| `/twitch canal` | Toca o áudio de uma live pública pelo nome do canal ou link |
| `/play busca` | Busca uma música pelo nome ou link e adiciona à fila |
| `/pause` | Pausa a reprodução atual |
| `/resume` | Retoma a reprodução pausada |
| `/skip` | Pula para a próxima faixa |
| `/stop` | Para a reprodução e limpa a fila |
| `/queue` | Mostra a fila e permite escolher uma faixa para tocar agora ou em seguida |
| `/leave` | Para a música, limpa a fila e desconecta o bot do canal de voz |
| `/slowmode segundos` | Configura o modo lento de um canal (0 a 21600 segundos) |
| `/lock` e `/unlock` | Bloqueia/desbloqueia o canal atual e restaura a permissão anterior |
| `/lockdown ativar` | Bloqueia ou restaura os canais de texto do servidor |
| `/setlogs canal` | Escolhe o canal para os logs de moderação |
| `/stream-add streamer canal` | Cadastra streamer e canal para anúncios de lives |
| `/stream-remove streamer` | Remove streamer monitorado |
| `/stream-list` | Lista os streamers cadastrados |
| `/stream-test streamer` | Testa o anúncio no canal configurado |
| `/configurar logs canal` | Define o canal de logs |
| `/configurar cargo cargo` | Autoriza um cargo a usar os comandos de moderação |
| `/configurar remover_cargo cargo` | Remove esse acesso |
| `/configurar cargo_moderador cargo` | Configura um cargo de moderador |
| `/configurar mutado cargo` | Registra o cargo; timeouts usam o recurso nativo do Discord |
| `/configurar filtro filtro ativo` | Ativa ou desativa filtros automáticos |
| `/configurar palavra acao palavra` | Adiciona/remove palavras bloqueadas |
| `/setup` | Mostra o estado da configuração de moderação |

## Usar comandos com prefixo

Todos os comandos slash também podem ser chamados como comandos de texto
usando `e!` no lugar de `/`. Por exemplo:

```text
e!ajuda
e!userinfo @membro
e!ping
e!play nome da música
e!twitch https://www.twitch.tv/nome_do_canal
e!despause
e!ban @membro motivo
e!timeout @membro 30m motivo
e!clear 10
e!clearglobal 123456789012345678
e!setlogs #mod-logs
e!configurar logs #mod-logs
e!configurar filtro anti_spam ativar
e!configurar palavra adicionar "palavra bloqueada"
```

Os subcomandos de configuração mantêm a estrutura: `e!configurar <opção>
<argumentos>`. Para argumentos com espaços, como motivos e nomes de músicas,
escreva o texto após os argumentos anteriores ou coloque-o entre aspas.
Os comandos com prefixo usam as mesmas verificações de permissões e hierarquia
dos comandos slash. O intent privilegiado **Message Content Intent** precisa
estar ativado no Developer Portal para o Discord entregar o conteúdo das
mensagens ao bot.

`/setlogs canal` (também `e!setlogs #canal`) define o destino dos registros;
quem executa precisa de **Gerenciar servidor**, e o bot precisa poder ver o
canal, enviar mensagens e incorporar links. O canal também pode ser definido
por `/configurar logs canal`. O bot registra banimentos, expulsões, timeouts,
avisos e exclusões de mensagens. Os registros incluem horário, IDs conhecidos,
moderador e motivo quando disponíveis. Para identificar moderadores e motivos
de ações feitas diretamente no Discord, conceda ao bot **Ver registro de
auditoria**; mensagens fora do cache podem não ter autor ou conteúdo disponíveis.

## Cargos, permissões e proteção

Quem tem a permissão Discord correspondente ao comando (ou `Administrador`)
pode usá-lo. Alternativamente, um administrador do servidor pode autorizar
cargos com `/configurar cargo`; esses cargos recebem acesso aos comandos de
moderação. Apenas quem tem **Gerenciar servidor** pode alterar as configurações.
O `/ajuda` verifica as permissões efetivas do Discord e também considera esse
cargo de moderador configurado, exibindo apenas as categorias acessíveis à
pessoa que executou o comando. Cada comando de moderação verifica o acesso
novamente quando executado; a ajuda não substitui essas verificações.

O bot também verifica as permissões dele, impede ações contra si mesmo, contra
o dono do servidor e contra membros com cargo igual ou superior ao moderador,
além de respeitar a hierarquia do próprio bot. Erros de permissão são enviados
de forma privada a quem executou o comando. Configure `/configurar logs canal`
para registrar ações em um canal de texto; o bot precisa poder ver e enviar
mensagens nesse canal. Para `/clear`, o bot também precisa de **Gerenciar
mensagens** e **Ver histórico de mensagens** no canal onde o comando for usado.
As mensagens recentes são apagadas em lote; mensagens com mais de 14 dias usam
exclusão individual, conforme as regras do Discord.

`e!clearglobal <message_id>` exige **Gerenciar mensagens** (ou um cargo de
moderador configurado) e, para o bot, **Ver canal**, **Ver histórico de
mensagens** e **Gerenciar mensagens** em todos os canais de texto e voz,
incluindo canais de palco. O comando procura o ID informado, depois compara o
conteúdo textual exato e o autor da mensagem em todo o histórico desses canais.
Ele apaga a mensagem de referência e todas as correspondências encontradas. Se
faltar acesso a qualquer canal ou a mensagem não tiver texto, a operação não
começa/apaga nada. A busca pode demorar em servidores com histórico extenso.

## Advertências, casos e automod

Cada advertência registra usuário, moderador, motivo e data. Ao chegar a 3
advertências ativas, o bot aplica timeout de 1 hora; o timeout se repete a cada
3 advertências. `/clearwarns` limpa as advertências ativas, mas mantém os casos
de auditoria consultáveis com `/case`.

Todos os filtros automáticos começam desativados. Ative-os em
`/configurar filtro`: anti-spam (5 mensagens em 5 segundos), anti-flood (8 em
10 segundos), excesso de menções (mais de 5), links, palavras configuradas,
mensagens repetidas (3 iguais em 30 segundos) e proteção contra raid (alerta
para 8 entradas em 10 segundos). Os filtros de mensagem apagam o conteúdo e
registram o motivo; não aplicam advertências nem timeout. A proteção contra
raid apenas alerta.

Para ler o conteúdo das mensagens, ative **Message Content Intent** em
**Bot > Privileged Gateway Intents** no Discord Developer Portal antes de
reiniciar o bot. Sem ele, o Discord pode impedir a conexão do bot, e os filtros
de conteúdo não funcionam. O intent de membros também é necessário para eventos
de entrada, saída e alterações de cargos.

O canal de logs registra entradas e saídas, expulsões identificadas pelo
registro de auditoria, banimentos, timeouts, mensagens editadas/apagadas,
alterações de cargos e apelidos e mudanças importantes do servidor. Para
identificar moderadores em ações realizadas fora dos comandos do bot, conceda
**Ver registro de auditoria** ao bot.

`/lock`, `/unlock` e `/lockdown` guardam a permissão anterior de envio de
mensagens e a restauram ao desbloquear. O modo lento usa a configuração nativa
do canal.

## Intents no portal

Ative **Server Members Intent** na página **Bot > Privileged Gateway Intents**
do Developer Portal. O bot usa esse intent para trabalhar com membros e cargos.
Ative também **Message Content Intent** para os filtros de palavras, links,
spam/flood e mensagens repetidas. Sem esse intent, esses filtros não podem ler
o conteúdo para moderar.
# Esquelote-corrigido
