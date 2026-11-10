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
menu lateral > Programação > Programação FOB > Cadastrar > Pesquisar > aguarda a tela de
Compartimentação (pode demorar minutos) > Pool > Placa > Buscar > Transportador >
carrega cada remessa no compartimento programado > Continuar > Agendamento (data/hora do
portal) > Finalizar.

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
    "tractorPlate" (vira a Placa), "trailerPlates", "driverName", "driverTaxId",
    "orders": [ { "orderId", "orderNumber", "distributorOrderNumber",
                  "stationName", "stationDistributorName" (vira o Pool; senão stationName),
                  "items": [ { "orderItemId", "productName", "qtyM3",
                               "compartments": [ { "number", "capacityM3" } ] } ] } ] } ] }
```
O GetDataModule gera **uma programação por viagem + pool** (pedidos do mesmo posto juntos).
Transportador = `VIBRA_TRANSPORTADOR` (padrão `BINDI`). Overrides para testes: `PLANEJADOS_MOCK_PATH` (JSON no formato acima,
ex. `planejados.example.json`) e `PROGRAMACOES_PATH` (CSV com colunas `pool`, `placa`, opcional
`transportador` - sem pedidos, não carrega compartimentos).
Saída: `programacoes_vibra.csv` (status `FINALIZADO`, `PREENCHIDO` - CSV sem pedidos - ou `ERRO`) + `erro_programacao_N.png`.
Agendamento: `main.py` repete o pipeline a cada `SCHEDULE_INTERVAL_MINUTES` (padrão 5; 0 = uma
vez). O CSV de saída é **acumulado** e serve de histórico: o GetDataModule pula viagem + pool já
`FINALIZADO` (o portal ainda devolve a viagem porque o reporte `registered_at_distributor` não
existe). Para refazer um teste, apague a linha (ou o arquivo) `programacoes_vibra.csv`.

Compartimentos: cada `items[].compartments[]` do portal é um compartimento alocado na viagem e
vai **cheio** (o portal não guarda volume por compartimento; volume = `capacityM3`). A Vibra
faz igual: carregar a remessa de 15 m³ no C5 (3 m³) deixa 12 m³ na remessa. `load_compartments`
casa a remessa pela Ordem + palavras do produto (portal "Etanol Hidratado" ⊆ Vibra "ETANOL
HIDRATADO COMBUSTIVEL"), confere capacidade e compartimento vazio, carrega e confere a Ordem.
`items[].qtyM3` é o total do item no pedido, não o da viagem.

**Pendências:**
- Tela depois do "Finalizar" (mensagem de sucesso/erro da Vibra) não confirmada via DOM:
  erros procurados em `.alert-danger`/`.ui-messages-error`. Motorista não é informado em
  nenhuma tela vista até aqui.
- Reporte ao portal (`registered_at_distributor` + observações) só depois que o salvar existir.
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
Menu lateral         app-menu-item > div.main-item > div.icone-texto > span.titulo
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
```
A tela de Programação FOB é JSF: selecionar via jQuery `.val().trigger('change')` (digitar no
select2 se perde na re-renderização) e esperar `jQuery.active` / `jsf.ajax` zerarem.

### Stack
Python 3.11+, `selenium`, `python-dotenv`, `requests`.

### Estrutura de pastas do projeto
```
├── main.py                          ← orquestrador (INIT → GET_DATA → PROCESS → END) em loop
├── vibra_programacao.py             ← protótipo original (referência)
├── Framework/
│   ├── InitModule.py                ← valida .env, credenciais e pré-requisitos
│   ├── GetDataModule.py             ← pedidos planejados do portal (ou PROGRAMACOES_PATH)
│   ├── ProcessModule.py             ← login → para cada programação abre o cadastro e preenche
│   ├── EndModule.py                 ← finalização (fecha o CSV, imprime resumo)
│   └── RPAFrameworkException.py     ← exceção controlada
├── Automation/
│   └── VibraPortal.py               ← passos Selenium (login + Programação FOB)
├── Utils/
│   ├── DriverFactory.py             ← cria o Chrome WebDriver + persiste cookies do SSO
│   ├── TokenApiClient.py            ← cliente da Vibra Token API (código de 2FA do e-mail)
│   └── PortalClient.py              ← cliente HTTP do Portal Bindi Log (pedidos planejados)
├── Log/
│   └── LogModule.py                 ← grava o resultado em programacoes_vibra.csv
├── programacoes.example.csv
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
`SCHEDULE_INTERVAL_MINUTES`. `CHROME_HEADLESS=1` fixo na imagem; 2FA só pela Vibra Token API
(não há quem digite o código).
**Volume obrigatório em `/data`**: o Dockerfile aponta `PROGRAMACOES_OUTPUT_PATH`,
`VIBRA_COOKIES_PATH` e `CHROME_USER_DATA_DIR` para lá. Sem Volume, cada deploy/restart apaga o
histórico de FINALIZADOS e as viagens seriam cadastradas de novo na Vibra.
Variáveis no dashboard do Railway (as do `.env.example`), nunca no Dockerfile.
