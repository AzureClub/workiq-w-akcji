# Architektura: run_agent.py — Work IQ + Azure AI Foundry

## Jak działa skrypt

Skrypt `run_agent.py` orkiestruje dwa niezależne systemy w dwóch krokach:

```
┌─────────────────────────────────────────────────────────────────┐
│                        run_agent.py                             │
│                                                                 │
│  KROK 1: Pobranie danych z kalendarza                           │
│  ┌───────────┐    subprocess     ┌───────────┐    M365 API      │
│  │  Python    │ ────────────────►│ workiq.cmd │ ──────────────►  │
│  │  script    │ ◄────────────────│ (CLI)      │ ◄──────────────  │
│  │           │    stdout (text)  └───────────┘  kalendarz JSON  │
│  │           │                                                   │
│  │  KROK 2: Analiza przez agenta Foundry                        │
│  │           │    HTTPS/REST     ┌────────────────────────┐     │
│  │           │ ────────────────►│ Azure AI Foundry         │     │
│  │           │ ◄────────────────│ agent: "workiqagent" v2  │     │
│  └───────────┘    response      └────────────────────────┘     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Krok 1 — `fetch_calendar_from_workiq()`

| Element          | Wartość                                                        |
|------------------|----------------------------------------------------------------|
| **Co wywołuje**  | `workiq.cmd ask -q "Jakie mam dzisiaj spotkania..."`           |
| **Jak**          | `subprocess.run()` — uruchamia proces potomny                 |
| **Gdzie działa** | Lokalnie na Twoim komputerze                                  |
| **Autentykacja** | Work IQ CLI loguje się do M365 przez Entra ID (przeglądarka)  |
| **Co zwraca**    | Tekst z listą spotkań (stdout procesu)                        |
| **Protokół**     | Żaden — to zwykłe wywołanie komendy systemowej                |

### Krok 2 — `ask_foundry_agent()`

| Element          | Wartość                                                        |
|------------------|----------------------------------------------------------------|
| **Co wywołuje**  | Azure AI Foundry — Responses API z `agent_reference`           |
| **Jak**          | `AIProjectClient` + `openai_client.responses.create()`         |
| **Gdzie działa** | Request idzie do Azure (chmura), agent przetwarza w Foundry    |
| **Autentykacja** | `DefaultAzureCredential` — az login / managed identity         |
| **Co zwraca**    | Odpowiedź agenta (analiza kalendarza)                          |
| **Protokół**     | HTTPS REST (OpenAI-compatible API)                             |

### Co dostaje agent Foundry?

Agent **nie ma** bezpośredniego dostępu do kalendarza. Dane są wklejone w prompt:

```
"Oto dane z mojego kalendarza pobrane z Work IQ:

  [... tekst z workiq ask ...]

Na podstawie tych danych odpowiedz na pytanie: Podsumuj mój dzisiejszy kalendarz."
```

To wzorzec **RAG-like** (Retrieve → Augment → Generate):
1. **Retrieve** — `workiq ask` pobiera kalendarz z M365
2. **Augment** — skrypt wkleja dane do promptu
3. **Generate** — agent Foundry analizuje i odpowiada

---

## Work IQ: CLI vs MCP — porównanie

| Cecha                  | `workiq ask` (CLI)                  | `workiq mcp` (MCP Server)                    |
|------------------------|-------------------------------------|-----------------------------------------------|
| **Typ**                | Jednorazowa komenda                 | Ciągły serwer (stdio)                         |
| **Protokół**           | Brak — stdin/stdout procesu         | Model Context Protocol (MCP)                  |
| **Wywołanie**          | `workiq ask -q "pytanie"`           | `workiq mcp` (startuje serwer)                |
| **Interakcja**         | Pytanie → odpowiedź → koniec        | Agent odpytuje dynamicznie wiele razy         |
| **Kto odpytuje**       | Skrypt przez `subprocess`           | Agent/IDE jako MCP client                     |
| **Dostęp do danych**   | Tylko to co zapytasz                | Agent sam decyduje kiedy i co odpytać         |
| **Konfiguracja**       | Prosta — jedno wywołanie            | Wymaga MCP client po stronie agenta/IDE       |
| **Użycie w skrypcie**  | ✅ Łatwe (`subprocess.run`)         | ⚠️ Wymaga MCP client library                 |
| **Użycie w VS Code**   | ❌ Nie nadaje się                   | ✅ Natywne wsparcie (Copilot Chat)            |
| **Użycie w Foundry**   | ✅ Przez orkiestrację (nasz skrypt) | ❓ Wymaga wsparcia MCP w Foundry              |
| **Autonomia agenta**   | Brak — skrypt decyduje co pobrać   | Pełna — agent sam odpytuje gdy potrzebuje      |

### Kiedy co wybrać?

**Użyj CLI (`workiq ask`)** gdy:
- Piszesz prosty skrypt / demo
- Chcesz ręcznie kontrolować co jest pobierane
- Agent nie obsługuje MCP

**Użyj MCP (`workiq mcp`)** gdy:
- Agent (Copilot, IDE, custom) obsługuje MCP client
- Chcesz, żeby agent sam decydował kiedy odpytać M365
- Budujesz interaktywnego asystenta z wieloma źródłami danych

---

## Uwierzytelnienie

Skrypt korzysta z **dwóch niezależnych mechanizmów autentykacji**:

### Work IQ CLI — Entra ID (MSAL, interaktywne)

```
Pierwsze wywołanie `workiq ask`:
  → Otwiera przeglądarkę z logowaniem Entra ID (M365)
  → Użytkownik loguje się + consent na uprawnienia
  → Token MSAL jest cachowany lokalnie

Kolejne wywołania:
  → Używa cached tokena (bez przeglądarki)
  → Gdy token wygaśnie → ponowne logowanie w przeglądarce

Wylogowanie:
  → `workiq logout` czyści cached tokeny
```

| Opcja CLI              | Co robi                                              |
|------------------------|------------------------------------------------------|
| `--account <email>`    | Wybiera konkretne konto z cache (multi-account)      |
| `workiq logout`        | Czyści cached tokeny                                 |
| `workiq config set`    | Ustawia domyślne wartości (np. tenant-id)            |

> ⚠️ **Ograniczenie**: Work IQ CLI wymaga interaktywnego logowania przez przeglądarkę.
> W kontekście automatyzacji (CI/CD, serwer bez GUI) skrypt zawiśnie czekając
> na logowanie. To rozwiązanie nadaje się do użytku deweloperskiego / demo.

### Azure AI Foundry — DefaultAzureCredential

```
DefaultAzureCredential próbuje po kolei:
  1. Environment variables (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, ...)
  2. Managed Identity (na Azure VM / Container App)
  3. Azure CLI (`az login`)
  4. Azure PowerShell (`Connect-AzAccount`)
  5. Interactive browser (fallback)
```

W praktyce na maszynie dewelopera wystarczy `az login` — dalej działa automatycznie.

### Podsumowanie auth w skrypcie

| Komponent        | Mechanizm                  | Interaktywny? | CI/CD-ready?  |
|------------------|----------------------------|---------------|---------------|
| Work IQ CLI      | Entra ID / MSAL (browser)  | ✅ Tak        | ❌ Nie        |
| Azure AI Foundry | DefaultAzureCredential     | Opcjonalnie   | ✅ Tak (MI/SP)|

---

## Potencjalne ulepszenia

1. **Dynamiczna ścieżka** — zamiana hardcoded `workiq.cmd` na `shutil.which("workiq")`
2. **MCP integration** — podłączenie `workiq mcp` jako tool do agenta Foundry
3. **Cachowanie** — unikanie wielokrotnego odpytywania Work IQ w jednej sesji
4. **Tryb interaktywny** — pętla pytanie-odpowiedź bez ponownego pobierania kalendarza
