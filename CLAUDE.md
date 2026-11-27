# Coding Standards — Priortec / Bindi - Cadastro

## Role
You are an assistant for Automation + AI application development at Priortec.
This project is **Bindi - Cadastro**: bot Python (Selenium) que cadastra Programação FOB no
Canal de Negócios Vibra para a BindiLog (detalhes abaixo).

## Language conventions
- **Code identifiers** (variables, methods, properties, interfaces, type literals) must be in **English**.
- **User-facing strings**, log messages, comments, and `print` output must be in **Portuguese**.

## Casing
- Python: `snake_case` for all identifiers.

## Classes and abstraction
- Create classes and abstractions only when genuinely necessary — not speculatively.
- Prefer flat functions and simple modules for single-responsibility logic.

## Error handling
- Always add proper exception handling so that automated processes fail loudly, not silently.
- Surface errors in Portuguese with enough context to diagnose.

## Comments
- Write comments only when the *why* is non-obvious. Never describe what well-named code already says.

---

## This project (Bindi - Cadastro)

### O que é
Bot Python (Selenium) que cadastra **Programação FOB** no **Canal de Negócios Vibra**
(`cn.vibraenergia.com.br`) para a operação da BindiLog. Origem: o script `vibra_programacao.py`
(protótipo standalone, login manual), agora reorganizado no padrão FrameworkRE do projeto
`C:\Priortec\Bindi - Vibra` (login SSO + 2FA, cookies e DriverFactory reaproveitados de lá).

Fluxo por programação (`Automation/VibraPortal.py`):
home (`https://cn.vibraenergia.com.br/home/#/`) > fecha o popup Informativos > ativa a unidade
fixa `VIBRA_UNIDADE` (padrão `10076 - POSTO ZEPPE SAO JOSE LTDA`: o Pool dela lista todos os
postos; compara sem acentos e também sem o código "10076 - ") > menu lateral > Programação >
Programação FOB > Cadastrar > Pesquisar > aguarda a tela de Compartimentação (pode demorar
minutos) > Pool do 1º posto > Placa > Buscar > Transportador > carrega cada remessa no
compartimento programado, **trocando o Pool antes dos compartimentos de cada posto** (confere
que a troca não desfez os já carregados) > Continuar > Agendamento (data/hora do portal; se o horário não estiver livre, o livre mais
próximo a até `MAX_SCHEDULE_TIME_DIFF_MINUTES` = 60 min, empate = o mais cedo, avisado na
mensagem ao portal; além disso ou dia sem horário = `RegistrationError`) >
Finalizar > Protocolo (número da programação).

Base funcional: ER Bindi Log (seções 3, 5.8 e 8.2 + BPMN). O tópico 11 "Fluxo Cadastro de
Transporte - Distribuidora Vibra" **está vazio** no .docx (V1 1 e V8): só o título, sem as telas.
Pela ER, o agente pega pedidos com status `planned` (Planejado), registra na Vibra os dados do
planejamento (caminhão, motorista, horário) e muda o status para `registered_at_distributor`.
Observações da Vibra (produto em análise, saldo insuficiente, site fora do ar) devem ser
listadas; "saldo insuficiente" reverte para `planned`.

Entrada: Portal Bindi Log `GET /api/rpa/vibra/planejados?days=N` (Bearer `PORTAL_API_TOKEN` =
`RPA_API_TOKEN` do portal, mesmo esquema de `pendentes`/`situacao` do Bindi - Vibra). Rota em
`C:\Priortec\Bindi-PAINEL\app\api\rpa\vibra\planejados\route.ts`.
No portal o planejamento é por **viagem** (`trip`) e não grava `transport_order.status='planned'`.
"Planejado" = viagem liberada (`released_at`, RN-16 garante conjunto + motorista + alocação),
com carregamento VIBRA (`trip_loading`) e compartimentos ainda `scheduled` (Programado), pedido
com nº na Vibra e status antes de `registered_at_distributor`; `trip.load_date` entre hoje e hoje + N.
```
{ "trips": [ {
    "tripId", "loadDate", "loadTime" (menor trip_loading VIBRA),
    "tractorPlate", "trailerPlates" (Placa na Vibra: a 1ª carreta; se a Vibra recusar - "veículo
    inválido" - tenta a próxima; sem carreta usa o cavalo),
    "driverName", "driverTaxId",
    "orders": [ { "orderId", "orderNumber", "distributorOrderNumber",
                  "stationName", "stationDistributorName" (vira o Pool; senão stationName),
                  "items": [ { "orderItemId", "productName", "qtyM3",
                               "compartments": [ { "number", "capacityM3" } ] } ] } ] } ] }
```
O GetDataModule gera **uma programação por viagem** (todos os postos dela; `order["vibraPool"]`
= Pool de cada pedido). O histórico (CSV) continua **por viagem + posto**: uma linha por posto com o
mesmo resultado, e posto já FINALIZADO na viagem fica fora da programação nova (só reenvia o resultado).
Transportador = `VIBRA_TRANSPORTADOR` (padrão `BINDI`). Overrides para testes: `PLANEJADOS_MOCK_PATH` (JSON no formato acima,
ex. `planejados.example.json`) e `PROGRAMACOES_PATH` (CSV com colunas `pool`, `placa`, opcional
`transportador` - sem pedidos, não carrega compartimentos).
Saída: `programacoes_vibra.csv` (status `FINALIZADO`, `PREENCHIDO` - CSV sem pedidos -, `ERRO_PORTAL`
- erro enviado ao portal - ou `ERRO` - erro técnico, tenta de novo) + `erro_programacao_N.png`.

Retorno ao portal: `POST /api/rpa/vibra/programacoes` (`Utils/PortalClient.report_registration`;
rota em `C:\Priortec\Bindi-PAINEL\app\api\rpa\vibra\programacoes\route.ts`), uma chamada por
viagem + pool: `{ tripId, distributorMessage?, distributorReference?, scheduledTime?, orders: [{ orderId, registered, observation?, technicalDetail? }] }`.
`distributorReference` = número da programação lido na tela de Protocolo depois do Finalizar.
`scheduledTime` ("HH:MM") = horário aceito pela Vibra; o portal grava em `trip_loading.distributor_scheduled_time`
(migração 034, 1º carregamento VIBRA da viagem) e mostra em "Carregamento na distribuidora" (com o planejado riscado
quando difere). No CSV fica no detail ("Agendado AAAA-MM-DD HH:MM") e é relido para o reenvio.
O portal grava o resultado **só na viagem** (`order_event.trip_id` + `distributor_reference`,
migrações 032 e 033 do portal; `lib/rpaTripRegistration.ts`) e **não muda o status do pedido**:
um pedido dividido em várias viagens é cadastrado viagem a viagem.
- Cadastrada -> a viagem sai de `planejados` (selo REG e "Prog. nº" no compartimento).
- `RegistrationError` (VibraPortal: horário indisponível, dia bloqueado, placa/posto não aceitos,
  pedido fora da Lista de Remessas, compartimento inexistente/ocupado/capacidade diferente, recusa no
  Finalizar) -> erro em aberto na viagem. No quadro de viagens aparece o
  selo ERRO no compartimento e um quadro com a mensagem, como corrigir, "Detalhes técnicos"
  (`technicalDetail`) e o botão "Tentar de novo". A viagem só volta para o agente depois que o
  usuário reabrir, corrigir e liberar (`trip.updated_at` mais novo que o erro) ou clicar "Tentar de
  novo" (fecha o erro), então o erro não repete a cada ciclo.
- Erro técnico (`RuntimeError`): tenta de novo nos próximos ciclos; na `MAX_TECHNICAL_ATTEMPTS`ª
  (3) seguida vai ao portal como erro com mensagem genérica de falha técnica.
- Mock (`PLANEJADOS_MOCK_PATH`) e `PROGRAMACOES_PATH` não enviam nada ao portal.
- **PROCESSANDO**: ao começar cada viagem (cadastro ou edição) o agente chama
  `POST /api/rpa/vibra/processando` `{ tripId, processing: true, action: 'register'|'edit' }`
  (`Utils/PortalClient.report_processing`; tabela `trip_rpa_processing`, migração 040 do portal) e,
  ao terminar (sucesso ou erro), `processing: false`. `programacoes`/`edicoes` também limpam a marca.
  No quadro de viagens aparece o selo PROCESSANDO no lugar de EDIÇÃO/ERRO; marca com mais de 30 min
  é ignorada (agente caiu no meio). O quadro recarrega sozinho a cada 30 s se houver viagem liberada.
  Falha nessa chamada só gera `[AVISO]`.
### Fila de edição (viagem já programada e alterada no portal)
Origem: o protótipo `vibra_edicao.py` (standalone, lia do Postgres), integrado ao pipeline.
No portal, reabrir + alterar + **liberar de novo** uma viagem que já tem nota de cadastro
(`order_event` `registered_at_distributor` com `trip_id`) abre um pedido de edição na tabela
`trip_registration_edit` (migração 035 do portal; `requestTripRegistrationEdit` em
`lib/rpaTripRegistration.ts`, chamado no `liberar`). Status: `pending` (fila do agente), `done`,
`error` (mensagem para o usuário; "Tentar de novo" ou liberar de novo voltam para `pending`).
Liberar de novo **sem mudar nada** do que vai para a Vibra (data de carga, conjunto, horário do
carregamento VIBRA, pedidos x compartimentos - motorista não conta) **não** abre edição: o portal compara
com a assinatura do último cadastro/edição (`trip_registration_signature` + `fn_trip_registration_signature`,
migração 037 do portal). Rede de segurança no bot: se mesmo assim chegar uma edição sem mudança real
(data/horário iguais e compartimentos já batendo - `compartments_already_match`), sai da tela sem
alterar e devolve `EDITADO` com o mesmo número.
Viagem com edição em aberto **não** sai em `planejados` (pedido novo incluído na edição viraria
outra programação). No quadro de viagens: selo EDIÇÃO no compartimento; erro de edição usa o
mesmo quadro de ERRO + "Tentar de novo".
- `GET /api/rpa/vibra/edicoes` (rota em `C:\Priortec\Bindi-PAINEL\app\api\rpa\vibra\edicoes\route.ts`):
  mesmo formato de `planejados` + `isEdit: true`, `editRequestId`, `distributorReference`
  (programação a alterar), `editRequestedAt`, `distributorScheduledTime` (horário aceito pela Vibra;
  **nulo quando o usuário alterou o carregamento** - o portal apaga o valor); a viagem inteira como
  está agora, `load_date >= hoje`.
  `planejados` devolve `isEdit: false`.
- `POST /api/rpa/vibra/edicoes` (`Utils/PortalClient.report_edit`): `{ editRequestId, tripId, edited,
  observation?, technicalDetail?, distributorMessage?, distributorReference?, scheduledTime? }`.
  Alterada: fecha o pedido e grava nota de cadastro (REG + nº) em todos os pedidos Vibra da viagem.
  Reenvio de pedido já `done` responde `alreadyDone`.

O agente processa a fila **depois dos cadastros**, na mesma sessão (`ProcessModule._edit_trip`):
home > unidade > menu Programação > Programação FOB > **Consultar** > Data Programada de ontem até
`VIBRA_EDIT_SEARCH_DAYS` (padrão 15) à frente (a data antiga da programação não é guardada no
portal) - a Vibra aceita no máximo **5 dias** por pesquisa (`MAX_QUERY_RANGE_DAYS`, dias
inclusos), então a janela é pesquisada em fatias de 5 dias até achar a programação -, Base =
`VIBRA_BASE` (padrão `BABET`, combo select2 `#formConProgcmbBase` que abre em outra base; `0` = mantém),
**Cliente = o posto da programação** (combo select2 `#formConProg:cmbCliente`, opções "17.272.550/0001-62 -
POSTO ZEPPE SAO JOSE LTDA - BABET"; a Consulta só lista as programações do cliente selecionado): escolhido
pelo **CNPJ** do posto (`orders[].stationTaxId` de `planejados`/`edicoes`; na exclusão, `stations[]` do pedido
de exclusão) ou, sem CNPJ igual, pelo nome. **A lista do Cliente depende da Base** (o JSF recarrega o combo
ao trocar a Base): o Cliente é escolhido **depois** da Base, como o usuário faz (`select_query_client`): clica
no select2, digita no campo de busca (CNPJ formatado "20.777.504/0001-20", depois o nome) e clica no
resultado. `find_schedule` tenta os postos da viagem na ordem dos Pools, cada um com todas as fatias de
datas; posto que não aparece no Cliente com essa Base (`ClientNotFoundError`, ex.: posto da AIBET) é
pulado > "Pesquisar Programações" > linha com o nº (pagina; `find_schedule` lê a coluna Agendamento "26/11/2026 01:00").
**A alteração da Vibra (lápis) não muda data/horário**, então `_schedule_change` decide:
- **Data mudou** (Agendamento x `loadDate`) ou **horário mudou** (carregamento alterado no portal =
  `distributorScheduledTime` nulo, e Agendamento x `loadTime`): `delete_schedule` (lixeira da linha
  `span#...:roleExcluirProgramacao a` > modal `#formConProg:confirmDialog` "Confirma a exclusão da
  programação N ?" > `a#formConProg:btnExcluir`, confere que a linha sumiu) > grava `EXCLUIDO`
  no CSV (`Programação excluída para recriar nº N`) > cadastra uma nova pelo fluxo do cadastro
  (`_register_trip`) > reporta a edição com o **número novo**. Se o cadastro novo falhar, a próxima
  tentativa vê o `EXCLUIDO` e só cadastra (a mensagem ao usuário avisa que a antiga já foi excluída).
  Horário diferente só porque a Vibra agendou no livre mais próximo **não** recria.
- **Placa mudou** (conjunto trocado na viagem): exclui e cadastra uma nova, igual à mudança de
  data/horário (`_delete_and_recreate`). Detectada **antes** do lápis por `_registered_plate`: placas
  na linha da Consulta (`find_schedule` devolve `plates`, se a Vibra mostrar) ou a placa gravada no
  CSV quando o agente cadastrou/editou a programação (`registeredPlate`, chave `("PLATE", nº)`).
  Sem nenhuma das duas, a tela de alteração confere: `fill_edit_registration` levanta
  `PlateChangedError` > volta à Consulta > exclui e recria.
- **Senão** (só compartimentos/pedidos): lápis "Alterar Programação" >
"Alterar" no modal > Pesquisa de Remessas > Pesquisar > "Alterar Programação - Compartimentação"
(`fill_edit_registration`): placa e transportador vêm **fixos** (texto "Placa: QUT8C39", sem campo,
Buscar ou `#cmbTranspp`) - só seleciona o Pool e confere a placa (placa trocada na viagem =
`RegistrationError`, a Vibra não deixa trocar na alteração). **Só mexe no que mudou**: compartimento
carregado que bate (ordem + produto) fica; o que diverge ou saiu da viagem é esvaziado na lixeira
(`unload_mismatched_compartments`, um por vez - o clique re-renderiza o `@form`); os novos/vazios são
carregados pelo fluxo normal (`load_compartments`). Se só a data mudou, nada é esvaziado nem carregado >
Agendamento com a data/horário atuais da programação (sem tela de horários, ou já selecionado =
só conclui) > Finalizar (na edição aceita também 'Alterar'/'Salvar'/'Confirmar') > a Vibra volta
para a Consulta com `div.alert-info` "Programação alterada com sucesso." (confirmado via DOM;
`EDIT_SUCCESS_PATTERN`) - vira a mensagem enviada ao portal; não há Protocolo, então vale o nº original. Histórico no CSV: status `EDITADO`, uma linha por edição, com
`Pedido de edição nº N` no detail - a chave do histórico da edição é esse nº (não viagem + posto);
`EDITADO` com o portal ainda devolvendo o pedido = só reenvia o resultado. Erros seguem as regras do
cadastro (`RegistrationError` direto ao portal, técnico após `MAX_TECHNICAL_ATTEMPTS`).
Simulação: `EDICOES_MOCK_PATH` (ex. `edicoes.example.json`); com `PLANEJADOS_MOCK_PATH` e sem ele
a fila não é consultada.

### Fila de exclusão (viagem excluída no portal)
"Excluir quadro" de uma viagem já cadastrada grava, **antes** de apagar a viagem (depois disso
`order_event.trip_id` vira nulo), um pedido de exclusão na tabela `trip_registration_deletion`
(migração 038 do portal, sem FK para `trip`; `requestTripRegistrationDeletion` em
`lib/rpaTripRegistration.ts`, chamado no `DELETE /api/controle/viagens/[id]`) com o nº da última
programação e a data de carga. A confirmação do "Excluir quadro" avisa que a programação na Vibra
também será excluída.
- `GET /api/rpa/vibra/exclusoes` → `{ deletions: [{ deletionRequestId, tripId, distributorReference,
  loadDate, tractorPlate, requestedAt }] }` (só `pending`).
- `POST /api/rpa/vibra/exclusoes` (`Utils/PortalClient.report_deletion`): `{ deletionRequestId,
  deleted, observation?, technicalDetail? }` → `done` / `error` (resultado fica só no pedido; a
  viagem já não existe - **ainda não há tela no portal para os erros**, só o log do agente).
O agente processa a fila **depois das edições** (`ProcessModule._delete_trip_schedule`): Consultar >
`find_schedule` (fatias de 5 dias em torno da data de carga) > `delete_schedule`. Programação não
encontrada (`ScheduleNotFoundError`) = já não está na Vibra → `done` com observação. CSV: status
`EXCLUIDO` com `Pedido de exclusão nº N` no detail (histórico por pedido de exclusão; `EXCLUIDO` com o
portal ainda devolvendo = só reenvia). Erros seguem as regras do cadastro. Em simulação (mocks) a fila
não é consultada. Viagens excluídas **antes** desta mudança (ex.: programação 4041613585) não entram na
fila: excluir manualmente na Vibra.

Agendamento: `main.py` repete o pipeline a cada `SCHEDULE_INTERVAL_MINUTES` (padrão 5; 0 = uma
vez). O CSV de saída é **acumulado** e serve de histórico: viagem + pool já `FINALIZADO` nunca é
cadastrada de novo na Vibra - se o portal ainda a devolve (retorno falhou), só o resultado é
reenviado, sem abrir o navegador. Também conta os `ERRO` técnicos seguidos (zera em `ERRO_PORTAL`).
Para refazer um teste, apague a linha (ou o arquivo) `programacoes_vibra.csv`.

Compartimentos: cada `items[].compartments[]` do portal é um compartimento alocado na viagem e
vai **cheio** (o portal não guarda volume por compartimento; volume = `capacityM3`). A Vibra
faz igual: carregar a remessa de 15 m³ no C5 (3 m³) deixa 12 m³ na remessa. `load_compartments`
casa a remessa pela Ordem + palavras do produto (portal "Etanol Hidratado" ⊆ Vibra "ETANOL
HIDRATADO COMBUSTIVEL"), confere capacidade e compartimento vazio, carrega e confere a Ordem.
`items[].qtyM3` é o total do item no pedido, não o da viagem.

**Pendências:**
- Tela de erro depois do "Finalizar" não confirmada via DOM: erros procurados em
  `.alert-danger`/`.ui-messages-error`. Motorista não é informado em nenhuma tela vista até aqui.
- Fila de edição: telas Consultar / Alterar Programação **não confirmadas via DOM** (seletores do
  protótipo, por rótulo/texto): modal 'Alterar', a Pesquisa de Remessas da alteração, o botão final
  da alteração e se o horário da própria programação aparece livre no Agendamento.
- Saldo insuficiente / produto em análise (ER: reverte para `planned` sem erro) ainda não é
  identificado na Vibra: hoje cai em "pedido não disponível na Lista de Remessas" (`scheduling_error`).
- Preencher `VIBRA_TOKEN_API_TOKEN` (= `API_TOKEN` do serviço `bindi_email_token_reader` no
  Railway, `https://emailtokenreader.up.railway.app`, padrão de `VIBRA_TOKEN_API_URL`) e
  confirmar que o e-mail de 2FA da conta `VIBRA_USUARIO` chega no Gmail lido pela API.

### 2FA por e-mail (integração Bindi-Email)
O login da Vibra pede um código enviado por e-mail. Quem lê o código é a **Vibra Token API**
(`C:/Priortec/Bindi-Email/api`, IMAP no Gmail), via `Utils/TokenApiClient.py`:
`POST /token/prepare` (ticket, **antes** de clicar em `#loginBt`) → envia usuário/senha →
`GET /token?ticket=&timeout_seconds=TOKEN_TIMEOUT_S` (long-poll) → digita o código.
Config: `VIBRA_TOKEN_API_URL`, `VIBRA_TOKEN_API_TOKEN` (= `API_TOKEN` da API), `TOKEN_TIMEOUT_S`.
O InitModule checa `/health` (`VIBRA_TOKEN_API_URL=0` desliga a API). Sem a API, ou se o código não chegar /
não for aceito, cai no modo manual: código digitado na janela ou gravado em `mfa_code.txt`
(`MFA_WAIT_SECONDS`).

### Navegação no portal (seletores do protótipo)
```
Login (SSO)          #username, #password, #loginBt
Popup Informativos   app-informativo-lateral i.md-icons 'close' (fechar antes de usar o menu)
Unidade ativa        div.dados-cliente span.nome (nome completo no textContent)
Trocar unidade       i.trigger-grupo-empresa -> div.grupo-empresa (div.razao-social span +
                     input[type=radio]); div.footer-grupo-empresa button 'Trocar' / 'Cancelar'
Menu "NOVA HOME"     home-acesso-rapido-lateral; expande clicando em mat-icon.icone-expandir
Menu lateral antigo  app-menu-item > div.main-item > div.icone-texto > span.titulo
  (Angular)          recolhido = só ícones; expande no hover (span.titulo só existe expandido).
                     Subitens em div.subitem (animação expandCollapse, altura 0 -> auto).
                     'Programação' > 'Programação FOB' > 'Cadastrar' (pode abrir nova aba;
                     estrutura do 'Cadastrar' ainda não confirmada via DOM)
Pesquisa de Remessas #forumlarioPesquisa:comboBase; input[type=submit][value=Pesquisar] (o visível)
Compartimentação     #comboPool (select2), placa = input text ao lado de input[value=Buscar],
                     #cmbTranspp (select2)
Lista de Remessas    tr#tblRemessas:N:row: td Ação (button[data-toggle=modal], data-target =
                     id do modal), Produto, Data Limite, Volume, Ordem (= distributorOrderNumber)
Modal compartimento  a[title='Carregar neste compartimento'] com o número (onclick mojarra)
Carregamento Veículo tr#tblVeiculo:N:row: Ação, Compartimento, Capacidade (select), Data Limite,
                     Produto, Ordem; botão a.btn-primary 'Continuar'
Agendamento          Bootstrap Datepicker inline #datepickid .datepicker-days: td.day
                     (old/new = outro mês, disabled = bloqueado; th.prev/th.next/
                     th.datepicker-switch 'Novembro 2026') - clicar sempre no dia;
                     select#combo_horarios (option value "16:00", texto "06/11/2026 - 16:00",
                     onchange mojarra.ab); a#botao_cria1 'Finalizar'
Consulta (edição)    rótulos 'Data Programada' (2 inputs); Cliente = select2 #formConProg:cmbCliente
                     (posto; '17.272.550/0001-62 - POSTO ... - BABET'); Base = select2 #formConProgcmbBase
                     ('BABET (5025) - BETIM - MG'; confirmado via DOM); botão 'Pesquisar
                     Programações'; sem resultado = aviso 'Nenhuma programação foi encontrada.'
                     Linhas tr#formConProg:tblProgramacoesMD:N:row (nº em qualquer td); lápis =
                     <a onclick="mojarra.ab(...openDialogModalAlterar)"> com button[title='Alterar
                     Programação'] > i.fa-pencil-square-o (confirmado via DOM; clicar no <a>).
                     Lixeira: span#...:roleExcluirProgramacao > a (onclick confirmDialog) >
                     button[title='Excluir Programação']; modal #formConProg:confirmDialog >
                     a#formConProg:btnExcluir 'Excluir' (confirmados via DOM).
                     Modal 'Alterar' e Pesquisa de Remessas da alteração não confirmados via DOM
Alterar Compartim.   (confirmado via DOM) 'Placa: X' e Transportador em texto fixo; #comboPool;
                     carregado: td Ação > a[title='Excluir produto do compartimento'] (onclick
                     mojarra '@form') > button.btn-danger (lixeira); vazio: 'Vazio' + select de
                     capacidade; botão verde 'Continuar' (rolar até ele)
Protocolo            'Programação criada com sucesso!' + h2 'Número da Programação : 4041588304 -
                     19/11/2026 18:15' (vai no detail do CSV como 'Programação Vibra nº N')
```
Timeouts: `VIBRA_TIMEOUT_S` (padrão 30, elementos comuns) e `VIBRA_SLOW_SCREEN_TIMEOUT_S`
(padrão 600, telas lentas + AJAX). Lidos no import do VibraPortal (que chama `load_dotenv()`
por isso); validados no InitModule.
A tela de Programação FOB é JSF: selecionar via jQuery `.val().trigger('change')` (digitar no
select2 se perde na re-renderização) e esperar `jQuery.active` / `jsf.ajax` zerarem.

### Stack
Python 3.11+, `selenium`, `python-dotenv`, `requests`.

### Estrutura de pastas do projeto
```
├── main.py                          ← orquestrador (INIT → GET_DATA → PROCESS → END) em loop
├── vibra_programacao.py             ← protótipo original do cadastro (referência)
├── vibra_edicao.py                  ← protótipo original da edição (referência; integrado ao pipeline)
├── Framework/
│   ├── InitModule.py                ← valida .env, credenciais e pré-requisitos
│   ├── GetDataModule.py             ← pedidos planejados + fila de edição do portal (ou mocks/CSV)
│   ├── ProcessModule.py             ← login → cadastra, depois edita e por fim exclui (filas do portal)
│   ├── EndModule.py                 ← finalização (fecha o CSV, imprime resumo)
│   └── RPAFrameworkException.py     ← exceção controlada
├── Automation/
│   └── VibraPortal.py               ← passos Selenium (login + Programação FOB Cadastrar/Consultar)
├── Utils/
│   ├── DriverFactory.py             ← cria o Chrome WebDriver + persiste cookies do SSO
│   ├── TokenApiClient.py            ← cliente da Vibra Token API (código de 2FA do e-mail)
│   └── PortalClient.py              ← cliente HTTP do Portal Bindi Log (planejados + resultado)
├── Log/
│   └── LogModule.py                 ← grava o resultado em programacoes_vibra.csv
├── programacoes.example.csv
├── planejados.example.json / edicoes.example.json   ← mocks do portal
├── Dockerfile / railway.json / .dockerignore   ← deploy no Railway
├── requirements.txt
├── .env                              ← não versionar
└── .env.example
```

---

## FrameworkRE — como funciona

Padrão **FrameworkRE** adaptado para Python pela Priortec. Pipeline linear de 4 módulos.
**Reutilize este padrão em todos os projetos de automação Priortec.**

### Pipeline: INIT → GET_DATA → PROCESS → END

```
main.py
  └── InitModule.initialize()        # 1. Valida ambiente (falha total se erro)
  └── GetDataModule.get_data()       # 2. Carrega itens; None = processar tudo
  └── ProcessModule.process_items()  # 3. try/except isolado por item
  └── EndModule.finalize()           # 4. sempre executa (finally)
```

### Módulos e responsabilidades

**InitModule** — valida variáveis de ambiente obrigatórias, existência de paths e pré-requisitos.
Levanta `RPAFrameworkException` para falhas que impedem qualquer execução. Nunca abre browser ou
lê dados de negócio aqui.

**GetDataModule** — única responsabilidade: ler a fonte de dados e devolver `list[dict]` ou `None`.
Filtra itens já processados. Nunca escreve.

**ProcessModule** — itera sobre os itens; cada item tem seu próprio `try/except` (erro em um item
não para o loop). Chama `LogModule` para persistir STATUS e RESULTADO. Instancia scanners/copiers
dentro do loop para evitar estado compartilhado.

**EndModule** — executado no `finally` de `Main.run()`. Fecha recursos, imprime resumo.

**RPAFrameworkException** — para erros controlados (configuração errada, pré-requisito faltando).
Esses erros terminam o processo inteiro. Erros de negócio por item usam `Exception` comum.

**LogModule** — único módulo que escreve na planilha (`openpyxl`). Mantém workbook aberto durante
toda a execução. `write_value(row_index, column_name, value)`. Fecha no `EndModule`.

### Regras do FrameworkRE
1. Um módulo, uma responsabilidade.
2. Falha ruidosa: todo `except` loga com contexto antes de relançar ou registrar.
3. Estado isolado por item: instanciar recursos dentro do loop.
4. LogModule é o único que escreve na planilha.
5. Erros em um item não param o pipeline — registram e continuam.

---

## Estilo CLI
- Separadores `"=" * 55` entre clientes.
- Prefixos: `[OK]`, `[INFO]`, `[AVISO]`, `[ERRO]`, `[ERRO Framework]`.
- Não usar `logging` do Python — `print` direto conforme padrão Priortec.

## Deploy
Railway, serviço **contínuo** (não é cron): `Dockerfile` (`python:3.11-slim-bookworm` + Google
Chrome do .deb oficial, instalado COM recommends - sem eles o chromedriver morre com status 127)
+ `railway.json` (`restartPolicyType: ON_FAILURE`). O `main.py` repete o ciclo a cada
`SCHEDULE_INTERVAL_MINUTES`. `CHROME_HEADLESS=1` fixo na imagem (a Akamai da Vibra dá "Access Denied" para `HeadlessChrome` no
User-Agent; o `DriverFactory` troca por `Chrome` em headless); 2FA só pela Vibra Token API
(não há quem digite o código).
**Volume obrigatório em `/data`**: o Dockerfile aponta `PROGRAMACOES_OUTPUT_PATH`,
`VIBRA_COOKIES_PATH` e `CHROME_USER_DATA_DIR` para lá. Sem Volume, cada deploy/restart apaga o
histórico de FINALIZADOS e as viagens seriam cadastradas de novo na Vibra.
Variáveis no dashboard do Railway (as do `.env.example`), nunca no Dockerfile.
